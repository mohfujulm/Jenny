from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings
from app.datastore import rebuild_semantic_index


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Build the local semantic document index.")
    parser.add_argument(
        "--input",
        dest="input_path",
        default=str(settings.docstore_json_path),
        help="Path to the source JSON document corpus.",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        default=str(settings.semantic_index_path),
        help="Path to the output SQLite semantic index.",
    )
    parser.add_argument(
        "--embedding-model",
        "--search-embedding-model",
        dest="search_embedding_model",
        default=settings.semantic_search_embedding_model,
        help="Embedding model to use for document search and retrieval.",
    )
    parser.add_argument(
        "--dimensions",
        "--search-dimensions",
        dest="search_dimensions",
        type=int,
        default=settings.semantic_search_embedding_dimensions,
        help="Optional dimensions override for search embeddings.",
    )
    parser.add_argument(
        "--answer-embedding-model",
        default=settings.semantic_answer_embedding_model,
        help="Embedding model to use for agent answer-time retrieval.",
    )
    parser.add_argument(
        "--answer-dimensions",
        type=int,
        default=settings.semantic_answer_embedding_dimensions,
        help="Optional dimensions override for answer embeddings.",
    )
    parser.add_argument(
        "--chunk-size-words",
        type=int,
        default=settings.semantic_chunk_size_words,
        help="Maximum words per chunk.",
    )
    parser.add_argument(
        "--chunk-overlap-words",
        type=int,
        default=settings.semantic_chunk_overlap_words,
        help="Chunk overlap in words.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=settings.semantic_embedding_batch_size,
        help="Embedding batch size.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()

    result = rebuild_semantic_index(
        source_path=Path(args.input_path),
        index_path=Path(args.output_path),
        openai_api_key=settings.openai_api_key,
        search_embedding_model=args.search_embedding_model,
        search_embedding_dimensions=args.search_dimensions,
        answer_embedding_model=args.answer_embedding_model,
        answer_embedding_dimensions=args.answer_dimensions,
        chunk_size_words=args.chunk_size_words,
        chunk_overlap_words=args.chunk_overlap_words,
        batch_size=args.batch_size,
    )

    print(f"Indexed {result['documents_indexed']} document(s)")
    print(f"Indexed {result['chunks_indexed']} chunk(s)")
    print(f"Search embedding model: {result['search_embedding_model']}")
    print(f"Answer embedding model: {result['answer_embedding_model']}")
    print(f"Index path: {result['index_path']}")


if __name__ == "__main__":
    main()
