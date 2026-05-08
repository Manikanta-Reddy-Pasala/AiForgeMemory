with open("aiforge_memory/query/bundle.py") as f:
    content = f.read()

import re
content = re.sub(
    r"def _notes_for\(.*?\).*?return \[\]\n",
    """def _notes_for(
    driver, *, repo: str, paths: list[str], fqnames: list[str], limit: int = 5,
) -> list[dict]:
    cy = (
        "MATCH (n:Note_v2 {repo:$repo}) "
        "WHERE EXISTS { MATCH (n)-[:MENTIONS]->(f:File_v2 {repo:$repo}) "
        "               WHERE f.path IN $paths } OR "
        "      EXISTS { MATCH (n)-[:MENTIONS]->(s:Symbol_v2 {repo:$repo}) "
        "               WHERE s.fqname IN $fqnames } "
        "RETURN n.id AS id, coalesce(n.title,'') AS title, "
        "       coalesce(n.body,'') AS body, coalesce(n.tags,[]) AS tags "
        "ORDER BY n.created_at DESC LIMIT $limit"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(
                cy, repo=repo, paths=paths or [""], fqnames=fqnames or [""], limit=limit
            )]
    except Exception:
        return []
""",
    content,
    flags=re.DOTALL
)

content = re.sub(
    r"def _docs_for\(.*?\).*?return \[\]\n",
    """def _docs_for(
    driver, *, repo: str, paths: list[str], fqnames: list[str], limit: int = 5,
) -> list[dict]:
    cy = (
        "MATCH (d:Doc_v2 {repo:$repo}) "
        "WHERE EXISTS { MATCH (d)-[:MENTIONS]->(f:File_v2 {repo:$repo}) "
        "               WHERE f.path IN $paths } OR "
        "      EXISTS { MATCH (d)-[:MENTIONS]->(s:Symbol_v2 {repo:$repo}) "
        "               WHERE s.fqname IN $fqnames } "
        "RETURN d.id AS id, coalesce(d.title,'') AS title, "
        "       coalesce(d.body,'') AS body, coalesce(d.url,'') AS url "
        "ORDER BY d.created_at DESC LIMIT $limit"
    )
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run(
                cy, repo=repo, paths=paths or [""], fqnames=fqnames or [""], limit=limit
            )]
    except Exception:
        return []
""",
    content,
    flags=re.DOTALL
)

with open("aiforge_memory/query/bundle.py", "w") as f:
    f.write(content)
