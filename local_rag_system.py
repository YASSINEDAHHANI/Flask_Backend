import os
import logging
import json
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import warnings

# Core libraries
import chromadb
from pypdf import PdfReader
import PyPDF2
from sentence_transformers import SentenceTransformer
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain.chains import RetrievalQA
from langchain.prompts import PromptTemplate
from langchain.schema import Document
from langchain_openai import AzureChatOpenAI

# Suppress warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class LocalRAGSystem:
    """
    Local RAG System with Azure OpenAI integration for document Q&A
    """

    def __init__(self,
                 pdf_folder: str,
                 persist_directory: str = "./chroma_db_local",
                 ollama_model: str = "qwen3:8b",  # legacy param, ignored
                 embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
                 chunk_size: int = 1000,
                 chunk_overlap: int = 200):
        """
        Initialize the Local RAG System

        Args:
            pdf_folder: Path to folder containing PDF documents
            persist_directory: Directory to store vector database
            ollama_model: Legacy parameter (kept for backward compat, ignored)
            embedding_model: HuggingFace embedding model
            chunk_size: Size of text chunks
            chunk_overlap: Overlap between chunks
        """
        self.pdf_folder = Path(pdf_folder)
        self.persist_directory = persist_directory
        self.ollama_model = ollama_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        # Initialize components
        self._setup_components()
        self._setup_llm()
        self._setup_vector_store()
        self._setup_qa_chain()

        logger.info("Local RAG System initialized successfully")

    def _setup_components(self):
        """Setup core components"""
        try:
            # Text splitter
            self.text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                separators=["\n\n", "\n", " ", ""]
            )

            # Embedding model
            self.embeddings = HuggingFaceEmbeddings(
                model_name="sentence-transformers/all-MiniLM-L6-v2"
            )

            logger.info("Core components setup complete")

        except Exception as e:
            logger.error(f"Error setting up components: {e}")
            raise

    def _setup_llm(self):
        """Setup Azure OpenAI LLM"""
        try:
            self.llm = AzureChatOpenAI(
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
                azure_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT"),
                temperature=0.1,
                model_kwargs={"max_completion_tokens": 4096},
            )
            # Connection test
            test_response = self.llm.invoke("Hello")
            logger.info(
                f"Azure OpenAI deployment '{os.getenv('AZURE_OPENAI_CHAT_DEPLOYMENT')}' connected successfully"
            )

        except Exception as e:
            logger.error(f"Error connecting to Azure OpenAI: {e}")
            logger.error("Check AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, and AZURE_OPENAI_CHAT_DEPLOYMENT in .env")
            raise

    def _setup_vector_store(self):
        """Setup or load vector store"""
        try:
            if os.path.exists(self.persist_directory):
                self.vector_store = Chroma(
                    persist_directory=self.persist_directory,
                    embedding_function=self.embeddings
                )
                logger.info("Loaded existing vector store")
            else:
                self.vector_store = None
                logger.info("Vector store will be created when documents are processed")

        except Exception as e:
            logger.error(f"Error setting up vector store: {e}")
            raise

    def _setup_qa_chain(self):
        """Setup QA chain with custom prompt"""
        template = """
        Contexte: {context}

        Question: {question}

        Instructions:
        - Réponds à la question en utilisant uniquement le contexte fourni ci-dessus
        - Réponds en français de manière claire et concise
        - Si l'information n'est pas présente dans le contexte, dis simplement "Je ne trouve pas cette information dans les documents fournis"
        - Cite les sources quand c'est pertinent
        - Sois précis et factuel

        Réponse:
        """

        self.prompt = PromptTemplate(
            template=template,
            input_variables=["context", "question"]
        )

        logger.info("QA chain setup complete")

    def extract_text_from_pdf(self, pdf_path: Path) -> str:
        """Extract text from PDF with error handling"""
        text = ""
        try:
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                text += page.extract_text() + "\n"
        except Exception as e1:
            try:
                with open(pdf_path, 'rb') as file:
                    pdf_reader = PyPDF2.PdfReader(file)
                    for page in pdf_reader.pages:
                        text += page.extract_text() + "\n"
            except Exception as e2:
                logger.error(f"Error extracting text from {pdf_path}: {e1}, {e2}")
                return ""

        return text.strip()

    def extract_pdf_metadata(self, pdf_path: Path) -> dict:
        """Extract comprehensive metadata from PDF"""
        try:
            reader = PdfReader(pdf_path)
            info = reader.metadata

            metadata = {
                "filename": pdf_path.name,
                "path": str(pdf_path),
                "title": info.title if info and info.title else pdf_path.stem,
                "author": info.author if info and info.author else None,
                "subject": info.subject if info and info.subject else None,
                "creator": info.creator if info and info.creator else None,
                "producer": info.producer if info and info.producer else None,
                "creation_date": str(info.creation_date) if info and info.creation_date else None,
                "modification_date": str(info.modification_date) if info and info.modification_date else None,
                "page_count": len(reader.pages),
                "file_size": pdf_path.stat().st_size,
                "processed_date": datetime.now().isoformat()
            }

            return metadata
        except Exception as e:
            logger.error(f"Error extracting metadata from {pdf_path}: {e}")
            return {
                "filename": pdf_path.name,
                "path": str(pdf_path),
                "title": pdf_path.stem,
                "error": str(e),
                "processed_date": datetime.now().isoformat()
            }

    def process_documents(self, recursive: bool = True) -> bool:
        """Process all PDF documents in the folder"""
        try:
            if recursive:
                pdf_files = list(self.pdf_folder.rglob("*.pdf"))
            else:
                pdf_files = list(self.pdf_folder.glob("*.pdf"))

            if not pdf_files:
                logger.warning(f"No PDF files found in {self.pdf_folder}")
                return False

            logger.info(f"Found {len(pdf_files)} PDF files to process")

            all_documents = []
            for i, pdf_file in enumerate(pdf_files, 1):
                logger.info(f"Processing {i}/{len(pdf_files)}: {pdf_file.name}")

                text = self.extract_text_from_pdf(pdf_file)
                if not text:
                    logger.warning(f"No text extracted from {pdf_file.name}")
                    continue

                metadata = self.extract_pdf_metadata(pdf_file)

                doc = Document(
                    page_content=text,
                    metadata=metadata
                )
                all_documents.append(doc)

            if not all_documents:
                logger.error("No documents were successfully processed")
                return False

            logger.info("Splitting documents into chunks...")
            chunks = self.text_splitter.split_documents(all_documents)
            logger.info(f"Created {len(chunks)} chunks")

            if self.vector_store is None:
                logger.info("Creating new vector store...")
                self.vector_store = Chroma.from_documents(
                    documents=chunks,
                    embedding=self.embeddings,
                    persist_directory=self.persist_directory
                )
            else:
                logger.info("Adding documents to existing vector store...")
                self.vector_store.add_documents(chunks)

            self.qa_chain = RetrievalQA.from_chain_type(
                llm=self.llm,
                chain_type="stuff",
                retriever=self.vector_store.as_retriever(
                    search_type="similarity",
                    search_kwargs={"k": 5}
                ),
                return_source_documents=True,
                chain_type_kwargs={"prompt": self.prompt}
            )

            logger.info("Document processing completed successfully")
            return True

        except Exception as e:
            logger.error(f"Error processing documents: {e}")
            return False

    def query_documents(self, question: str) -> Dict:
        """Query the document database"""
        try:
            if not hasattr(self, 'qa_chain'):
                raise ValueError("Documents not processed yet. Run process_documents() first.")

            result = self.qa_chain({"query": question})

            response = result["result"]
            source_docs = result["source_documents"]

            sources = []
            for i, doc in enumerate(source_docs, 1):
                source_info = {
                    "source_number": i,
                    "filename": doc.metadata.get('filename', 'Unknown'),
                    "title": doc.metadata.get('title', 'Unknown'),
                    "page_count": doc.metadata.get('page_count', 'Unknown'),
                    "text_preview": doc.page_content[:200] + "..." if len(doc.page_content) > 200 else doc.page_content
                }
                sources.append(source_info)

            return {
                "question": question,
                "response": response,
                "sources": sources,
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"Error querying documents: {e}")
            return {
                "question": question,
                "response": f"Erreur lors de la recherche: {str(e)}",
                "sources": [],
                "timestamp": datetime.now().isoformat()
            }

    def get_database_stats(self) -> Dict:
        """Get statistics about the document database"""
        try:
            if self.vector_store is None:
                return {"status": "No documents processed"}

            results = self.vector_store.get()

            if not results or not results.get('metadatas'):
                return {"status": "No documents in database"}

            metadatas = results['metadatas']
            filenames = [m.get('filename') for m in metadatas if m.get('filename')]
            unique_files = list(set(filenames))

            file_extensions = {}
            for filename in filenames:
                if filename:
                    ext = Path(filename).suffix.lower()
                    file_extensions[ext] = file_extensions.get(ext, 0) + 1

            total_size = sum(m.get('file_size', 0) for m in metadatas if m.get('file_size'))

            stats = {
                "total_chunks": len(results['ids']),
                "unique_files": len(unique_files),
                "file_list": unique_files,
                "file_extensions": file_extensions,
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "average_chunks_per_file": round(len(results['ids']) / len(unique_files), 1) if unique_files else 0,
                "last_updated": datetime.now().isoformat()
            }

            return stats

        except Exception as e:
            logger.error(f"Error getting database stats: {e}")
            return {"error": str(e)}

    def search_similar_documents(self, query: str, k: int = 5) -> List[Dict]:
        """Search for similar documents without generating response"""
        try:
            if self.vector_store is None:
                return []

            docs = self.vector_store.similarity_search(query, k=k)

            results = []
            for i, doc in enumerate(docs, 1):
                result = {
                    "rank": i,
                    "filename": doc.metadata.get('filename', 'Unknown'),
                    "title": doc.metadata.get('title', 'Unknown'),
                    "text_preview": doc.page_content[:300] + "..." if len(doc.page_content) > 300 else doc.page_content,
                    "metadata": doc.metadata
                }
                results.append(result)

            return results

        except Exception as e:
            logger.error(f"Error searching similar documents: {e}")
            return []

    def export_results(self, results: Dict, output_file: str):
        """Export search results to JSON file"""
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            logger.info(f"Results exported to {output_file}")
        except Exception as e:
            logger.error(f"Error exporting results: {e}")

    def generate_test_cases(self, requirement: str, context: str = "") -> str:
        """Generate test cases based on a requirement using Azure OpenAI"""
        try:
            test_case_prompt = f"""
Tu es un expert en génération de cas de test. Génère des cas de test détaillés en français pour l'exigence suivante.

Exigence: {requirement}
Contexte: {context}

IMPORTANT: Génère UNIQUEMENT les cas de test dans le format exact suivant, sans aucune explication supplémentaire:

**Cas fonctionnels**
Scenario (1) : [Titre du scénario]
Précondition : [Conditions préalables]
Étapes :
1. [Première étape]
2. [Deuxième étape]
3. [Troisième étape]
Résultat attendu : [Résultat espéré]

Scenario (2) : [Titre du scénario]
Précondition : [Conditions préalables]
Étapes :
1. [Première étape]
2. [Deuxième étape]
3. [Troisième étape]
Résultat attendu : [Résultat espéré]

[Continue avec 3-5 scénarios au total]

Génère des scénarios incluant:
- Cas de succès (fonctionnement normal)
- Cas d'erreur (données invalides)
- Cas limites (valeurs extrêmes)

Réponds UNIQUEMENT avec les cas de test formatés, rien d'autre.
"""

            response = self.llm.invoke(test_case_prompt)
            return response.content.strip()

        except Exception as e:
            logger.error(f"Error generating test cases: {e}")
            return f"Erreur lors de la génération des cas de test: {str(e)}"

    def interactive_mode(self):
        """Run interactive mode with test case generation as default"""
        print("\n" + "=" * 60)
        print("🧪 Générateur de Cas de Test - Mode Interactif")
        print("=" * 60)
        print("💡 Tapez votre exigence pour générer des cas de test")
        print("📄 Pour rechercher dans les documents: 'search: [question]'")
        print("Autres commandes: 'stats', 'help', 'quit'")
        print("-" * 60)

        while True:
            try:
                user_input = input("\n🎯 Exigence: ").strip()

                if user_input.lower() in ['quit', 'exit', 'q']:
                    print("👋 Au revoir!")
                    break

                elif user_input.lower() == 'stats':
                    stats = self.get_database_stats()
                    print("\n📊 Statistiques de la base de données:")
                    for key, value in stats.items():
                        print(f"  {key}: {value}")
                    continue

                elif user_input.lower() == 'help':
                    print("\n📝 Aide:")
                    print("  🧪 Génération de cas de test (défaut): 'L'utilisateur doit pouvoir se connecter'")
                    print("  📄 Recherche dans documents: 'search: Où trouve-t-on les agences?'")
                    print("  📊 Statistiques: 'stats'")
                    print("  ❌ Quitter: 'quit'")
                    continue

                elif user_input.startswith('search:'):
                    question = user_input[7:].strip()
                    if not question:
                        print("⚠️  Veuillez spécifier une question après 'search:'")
                        continue

                    print("🔍 Recherche dans les documents...")
                    result = self.query_documents(question)

                    print(f"\n💡 Réponse: {result['response']}")

                    if result['sources']:
                        print(f"\n📚 Sources ({len(result['sources'])}):")
                        for source in result['sources']:
                            print(f"  {source['source_number']}. {source['filename']}")
                    continue

                elif not user_input:
                    print("⚠️  Veuillez entrer une exigence.")
                    continue

                print("🧪 Génération des cas de test en cours...")
                test_cases = self.generate_test_cases(user_input)
                print()
                print(test_cases)

            except KeyboardInterrupt:
                print("\n👋 Au revoir!")
                break
            except Exception as e:
                print(f"❌ Erreur: {e}")


def main():
    """Main function to run the RAG system standalone (rarely used in deployment)"""
    default_path = os.getenv("LOCAL_PDF_FOLDER", "./pdfs")
    PDF_FOLDER = input(f"Chemin vers le dossier PDF (ou pressez Entrée pour '{default_path}'): ").strip() or default_path
    PERSIST_DIR = "./chroma_db_local"

    print(f"\n🚀 Initialisation du système RAG local...")
    print(f"📁 Dossier PDF: {PDF_FOLDER}")
    print(f"💾 Base de données: {PERSIST_DIR}")
    print(f"🤖 Modèle: Azure OpenAI ({os.getenv('AZURE_OPENAI_CHAT_DEPLOYMENT', 'unknown')})")

    try:
        rag = LocalRAGSystem(
            pdf_folder=PDF_FOLDER,
            persist_directory=PERSIST_DIR,
        )

        if not os.path.exists(PERSIST_DIR):
            print("\n📝 Traitement des documents PDF...")
            if rag.process_documents(recursive=True):
                print("✅ Documents traités avec succès!")
            else:
                print("❌ Erreur lors du traitement des documents")
                return
        else:
            print("✅ Base de données existante trouvée")
            rag.qa_chain = RetrievalQA.from_chain_type(
                llm=rag.llm,
                chain_type="stuff",
                retriever=rag.vector_store.as_retriever(
                    search_type="similarity",
                    search_kwargs={"k": 5}
                ),
                return_source_documents=True,
                chain_type_kwargs={"prompt": rag.prompt}
            )

        stats = rag.get_database_stats()
        print(f"\n📊 Base de données: {stats.get('unique_files', 0)} fichiers, {stats.get('total_chunks', 0)} chunks")

        rag.interactive_mode()

    except Exception as e:
        logger.error(f"Error in main: {e}")
        print(f"❌ Erreur: {e}")


if __name__ == "__main__":
    main()