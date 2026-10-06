import chromadb

client = chromadb.PersistentClient(path="./chroma_db")

collection = client.get_collection("langchain")

result = collection.get(
    include=["documents", "metadatas", "embeddings"]
)

for i, doc_id in enumerate(result["ids"]):
    print(f"\n{'=' * 50}")
    print(f"ID: {doc_id}")
    # print(f"Document: {result['documents'][i]}")
    print(f"Metadata: {result['metadatas'][i]}")

    # embedding = result["embeddings"][i]
    # print(f"向量维度: {len(embedding)}")
    # print(f"向量前 5 个值: {embedding[:5]}")