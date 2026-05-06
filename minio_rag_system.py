import os
import logging
import json
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import warnings
from io import BytesIO

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
from minio import Minio
from minio.error import S3Error

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class MinIORAGSystem:
    """
    MinIO-based RAG System with Azure OpenAI integration for document Q&A
    """

    def __init__(self,
                 minio_endpoint: str,
                 minio_access_key: str,
                 minio_secret_key: str,
                 bucket_name: str = "rag-documents",
                 persist_directory: str = "./chroma_db_minio",
                 ollama_model: str = "qwen3:8b",  # legacy, ignored
                 ollama_base_url: str = "http://localhost:11434",  # legacy, ignored
                 embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
                 chunk_size: int = 1000,
                 chunk_overlap: int = 200,
                 secure: bool = False):
        """
        Initialize the MinIO RAG System

        Args:
            minio_endpoint: MinIO server endpoint (e.g., 'minio:9000')
            minio_access_key: MinIO access key
            minio_secret_key: MinIO secret key
            bucket_name: MinIO bucket name for storing documents
            persist_directory: Directory to store vector database
            ollama_model: Legacy parameter (ignored, kept for backward compat)
            ollama_base_url: Legacy parameter (ignored, kept for backward compat)
            embedding_model: HuggingFace embedding model
            chunk_size: Size of text chunks
            chunk_overlap: Overlap between chunks
            secure: Use HTTPS for MinIO connection
        """
        self.bucket_name = bucket_name
        self.persist_directory = persist_directory
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        self.minio_client = Minio(
            minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            secure=secure
        )

        # Ensure bucket exists
        self._create_bucket_if_not_exists()

        # Initialize components
        self._setup_components()
        self._setup_vector_store()
        self._setup_qa_chain()

    def _create_bucket_if_not_exists(self):
        """Create bucket if it doesn't exist"""
        try:
            if not self.minio_client.bucket_exists(self.bucket_name):
                self.minio_client.make_bucket(self.bucket_name)
                logger.info(f"Bucket {self.bucket_name} created successfully")
            else:
                logger.info(f"Bucket {self.bucket_name} already exists")
        except S3Error as e:
            logger.error(f"Error with MinIO bucket: {e}")
            raise

    def _setup_components(self):
        """Initialize core components"""
        try:
            # Text splitter
            self.text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                separators=["\n\n", "\n", " ", ""]
            )

            # Embeddings
            self.embeddings = HuggingFaceEmbeddings(
                model_name="sentence-transformers/all-MiniLM-L6-v2",
                model_kwargs={'device': 'cpu'}
            )

            # Azure OpenAI LLM
            self.llm = AzureChatOpenAI(
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
                azure_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT"),
                temperature=0.1,
                max_tokens=4096,
            )

            logger.info("Components initialized successfully")

        except Exception as e:
            logger.error(f"Error setting up components: {e}")
            raise

    def _setup_vector_store(self):
        """Setup or load existing vector store"""
        try:
            if os.path.exists(self.persist_directory):
                logger.info("Loading existing vector store...")
                self.vector_store = Chroma(
                    persist_directory=self.persist_directory,
                    embedding_function=self.embeddings
                )
                logger.info("Vector store loaded successfully")
            else:
                logger.info("Vector store will be created when documents are processed")
                self.vector_store = None

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

        if self.vector_store is not None:
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

        logger.info("QA chain setup complete")

    def list_pdfs_in_bucket(self) -> List[str]:
        """List all PDF files in the MinIO bucket"""
        try:
            objects = self.minio_client.list_objects(self.bucket_name, recursive=True)
            pdf_files = [obj.object_name for obj in objects if obj.object_name.lower().endswith('.pdf')]
            return pdf_files
        except S3Error as e:
            logger.error(f"Error listing objects in bucket: {e}")
            return []

    def download_pdf_from_minio(self, object_name: str) -> bytes:
        """Download a PDF file from MinIO"""
        try:
            response = self.minio_client.get_object(self.bucket_name, object_name)
            data = response.read()
            response.close()
            response.release_conn()
            return data
        except S3Error as e:
            logger.error(f"Error downloading {object_name}: {e}")
            return b""

    def extract_text_from_pdf(self, pdf_data: bytes) -> str:
        """Extract text from PDF bytes"""
        text = ""
        try:
            pdf_stream = BytesIO(pdf_data)
            reader = PdfReader(pdf_stream)
            for page in reader.pages:
                text += page.extract_text() + "\n"
        except Exception as e1:
            try:
                pdf_stream = BytesIO(pdf_data)
                pdf_reader = PyPDF2.PdfReader(pdf_stream)
                for page in pdf_reader.pages:
                    text += page.extract_text() + "\n"
            except Exception as e2:
                logger.error(f"Error extracting text: {e1}, {e2}")
                return ""
        return text.strip()

    def extract_pdf_metadata(self, pdf_data: bytes, filename: str) -> dict:
        """Extract metadata from PDF bytes"""
        try:
            pdf_stream = BytesIO(pdf_data)
            reader = PdfReader(pdf_stream)
            info = reader.metadata

            metadata = {
                "filename": filename,
                "title": info.title if info and info.title else Path(filename).stem,
                "author": info.author if info and info.author else None,
                "subject": info.subject if info and info.subject else None,
                "creator": info.creator if info and info.creator else None,
                "producer": info.producer if info and info.producer else None,
                "creation_date": str(info.creation_date) if info and info.creation_date else None,
                "modification_date": str(info.modification_date) if info and info.modification_date else None,
                "page_count": len(reader.pages),
                "file_size": len(pdf_data),
                "processed_date": datetime.now().isoformat(),
                "source": "minio",
                "bucket": self.bucket_name
            }

            return metadata
        except Exception as e:
            logger.error(f"Error extracting metadata for {filename}: {e}")
            return {
                "filename": filename,
                "title": Path(filename).stem,
                "error": str(e),
                "processed_date": datetime.now().isoformat(),
                "source": "minio",
                "bucket": self.bucket_name
            }

    def process_documents_from_minio(self) -> bool:
        """Process all PDF documents from MinIO bucket"""
        try:
            pdf_files = self.list_pdfs_in_bucket()

            if not pdf_files:
                logger.warning(f"No PDF files found in bucket {self.bucket_name}")
                return False

            logger.info(f"Found {len(pdf_files)} PDF files in MinIO bucket")

            all_documents = []
            for i, pdf_file in enumerate(pdf_files, 1):
                logger.info(f"Processing {i}/{len(pdf_files)}: {pdf_file}")

                pdf_data = self.download_pdf_from_minio(pdf_file)
                if not pdf_data:
                    logger.warning(f"Could not download {pdf_file}")
                    continue

                text = self.extract_text_from_pdf(pdf_data)
                if not text:
                    logger.warning(f"No text extracted from {pdf_file}")
                    continue

                metadata = self.extract_pdf_metadata(pdf_data, pdf_file)

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

    def process_single_document(self, file_data: bytes, filename: str) -> bool:
        """Process a single document and add it to the vector store"""
        try:
            text = self.extract_text_from_pdf(file_data)
            if not text:
                logger.warning(f"No text extracted from {filename}")
                return False

            metadata = self.extract_pdf_metadata(file_data, filename)

            doc = Document(
                page_content=text,
                metadata=metadata
            )

            chunks = self.text_splitter.split_documents([doc])
            logger.info(f"Created {len(chunks)} chunks from {filename}")

            if self.vector_store is None:
                logger.info("Creating new vector store...")
                self.vector_store = Chroma.from_documents(
                    documents=chunks,
                    embedding=self.embeddings,
                    persist_directory=self.persist_directory
                )
            else:
                logger.info("Adding document to existing vector store...")
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

            logger.info(f"Document {filename} processed successfully")
            return True

        except Exception as e:
            logger.error(f"Error processing document {filename}: {e}")
            return False

    def query_documents(self, question: str) -> Dict:
        """Query the document database"""
        try:
            if not hasattr(self, 'qa_chain'):
                raise ValueError("Documents not processed yet. Run process_documents_from_minio() first.")

            result = self.qa_chain({"query": question})

            sources = []
            if "source_documents" in result:
                for doc in result["source_documents"]:
                    source_info = {
                        "filename": doc.metadata.get("filename", "Unknown"),
                        "title": doc.metadata.get("title", "Unknown"),
                        "page_count": doc.metadata.get("page_count", "Unknown"),
                        "content_preview": doc.page_content[:200] + "..." if len(doc.page_content) > 200 else doc.page_content
                    }
                    sources.append(source_info)

            return {
                "answer": result["result"],
                "sources": sources,
                "question": question,
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"Error querying documents: {e}")
            return {
                "error": str(e),
                "question": question,
                "timestamp": datetime.now().isoformat()
            }

    def get_database_stats(self) -> Dict:
        """Get statistics about the vector database"""
        try:
            if self.vector_store is None:
                return {"status": "Vector store not initialized", "total_chunks": 0, "unique_files": 0}

            results = self.vector_store.get()

            if not results or not results.get('metadatas'):
                return {"status": "No documents in database", "total_chunks": 0, "unique_files": 0}

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
                "last_updated": datetime.now().isoformat(),
                "source": "minio",
                "bucket": self.bucket_name
            }

            return stats

        except Exception as e:
            logger.error(f"Error getting database stats: {e}")
            return {"error": str(e)}

    def search_similar_documents(self, query: str, k: int = 5) -> List[Dict]:
        """Search for similar documents without generating answers"""
        try:
            if self.vector_store is None:
                return []

            docs = self.vector_store.similarity_search(query, k=k)

            results = []
            for doc in docs:
                result = {
                    "content": doc.page_content,
                    "metadata": doc.metadata,
                    "filename": doc.metadata.get("filename", "Unknown"),
                    "title": doc.metadata.get("title", "Unknown")
                }
                results.append(result)

            return results

        except Exception as e:
            logger.error(f"Error searching documents: {e}")
            return []

    def generate_test_cases(self, requirements: str, context: str = "") -> str:
        """Generate test cases based on requirements using the document knowledge base"""
        try:
            if hasattr(self, 'qa_chain'):
                # Use QA chain with document context
                query = f"""
                Generate test cases for the following software requirement. Use the document knowledge base for best practices and examples.

                Context: {context}
                Requirement: {requirements}

                Please provide comprehensive test cases including:
                1. Positive test scenarios
                2. Negative test scenarios
                3. Edge cases and boundary conditions
                4. Clear test steps and expected results

                Format each test case with:
                - Test Case Name
                - Description
                - Preconditions
                - Test Steps (numbered)
                - Expected Results
                """

                result = self.qa_chain({"query": query})
                return result["result"]

            # Fallback: direct LLM call without RAG context
            prompt = f"""
            Based on software testing best practices and the following requirement, generate comprehensive test cases.

            Context: {context}
            Requirement: {requirements}

            Please provide:
            1. Positive test cases (valid scenarios)
            2. Negative test cases (invalid scenarios)
            3. Edge cases and boundary conditions
            4. Clear test steps and expected results

            Format each test case with:
            - Test Case Name
            - Description
            - Preconditions
            - Test Steps
            - Expected Results
            """

            response = self.llm.invoke(prompt)
            return response.content

        except Exception as e:
            logger.error(f"Error generating test cases: {e}")
            try:
                prompt = f"""
                Generate test cases for this software requirement:

                Context: {context}
                Requirement: {requirements}

                Provide 5-8 comprehensive test cases with clear steps and expected results.
                """
                response = self.llm.invoke(prompt)
                return response.content
            except Exception as e2:
                logger.error(f"Fallback test case generation failed: {e2}")
                return f"Error generating test cases: {str(e2)}"

    def query(self, question: str) -> str:
        """Simple query method that returns just the answer text"""
        try:
            result = self.query_documents(question)
            if "error" in result:
                return f"Error: {result['error']}"
            return result["answer"]
        except Exception as e:
            logger.error(f"Error in query: {e}")
            return f"Error processing query: {str(e)}"


def create_minio_rag_system(
    minio_endpoint: str,
    minio_access_key: str,
    minio_secret_key: str,
    bucket_name: str = "rag-documents",
    ollama_base_url: str = None,  # legacy, ignored
) -> MinIORAGSystem:
    """Factory function to create a MinIO RAG System instance"""
    return MinIORAGSystem(
        minio_endpoint=minio_endpoint,
        minio_access_key=minio_access_key,
        minio_secret_key=minio_secret_key,
        bucket_name=bucket_name,
    )