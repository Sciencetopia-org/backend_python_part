from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse

import pyodbc
import requests
from bs4 import BeautifulSoup
from neo4j import GraphDatabase

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from ddgs import DDGS
except ImportError:  # Backward compatibility with the older package name.
    from duckduckgo_search import DDGS


ROOT_DIR = Path(__file__).resolve().parents[2]
APPSETTINGS_PATH = ROOT_DIR / "SciencetopiaWebApplication" / "appsettings.json"
DEFAULT_BLOCKED_DOMAINS = {
    "zhihu.com",
    "zhidao.baidu.com",
    "promotion.m1905.com",
}
TOKEN_STOPWORDS = {
    "the",
    "and",
    "for",
    "from",
    "into",
    "with",
    "this",
    "that",
    "what",
    "when",
    "where",
    "which",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
}


@dataclass(frozen=True)
class KnowledgeNode:
    name: str
    stable_id: str


@dataclass(frozen=True)
class ResourceCandidate:
    name: str
    link: str


@dataclass(frozen=True)
class StoredResource:
    id: str
    name: str
    link: str


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_appsettings() -> dict:
    if not APPSETTINGS_PATH.exists():
        return {}

    with APPSETTINGS_PATH.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def get_config_value(appsettings: dict, env_name: str, *path: str) -> str | None:
    env_value = os.getenv(env_name)
    if env_value:
        return env_value

    current = appsettings
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]

    return current if isinstance(current, str) and current else None


def normalize_sql_connection_string(connection_string: str) -> str:
    if "DRIVER=" in connection_string.upper():
        return connection_string

    normalized = connection_string
    normalized = re.sub(r"\bServer=", "SERVER=", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bInitial Catalog=", "DATABASE=", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bUser ID=", "UID=", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bPassword=", "PWD=", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bEncrypt=", "Encrypt=", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bTrustServerCertificate=", "TrustServerCertificate=", normalized, flags=re.IGNORECASE)

    ignored_keys = {
        "Persist Security Info",
        "MultipleActiveResultSets",
        "Connection Timeout",
    }
    parts = []
    for part in normalized.split(";"):
        if not part.strip() or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key not in ignored_keys:
            value = value.strip()
            if key in {"Encrypt", "TrustServerCertificate"}:
                value = {
                    "true": "yes",
                    "false": "no",
                }.get(value.lower(), value)
            parts.append(f"{key}={value}")

    return "DRIVER={ODBC Driver 18 for SQL Server};" + ";".join(parts) + ";"


def open_sql_connection(connection_string: str) -> pyodbc.Connection:
    return pyodbc.connect(normalize_sql_connection_string(connection_string), timeout=30, autocommit=False)


def detect_resource_table(conn: pyodbc.Connection) -> tuple[str, set[str]]:
    rows = conn.cursor().execute(
        """
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'KnowledgeGraph'
          AND TABLE_NAME IN ('Resources', 'Resource')
        ORDER BY CASE WHEN TABLE_NAME = 'Resources' THEN 0 ELSE 1 END
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("Cannot find KnowledgeGraph.Resources or KnowledgeGraph.Resource.")

    table_name = rows[0][0]
    column_rows = conn.cursor().execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'KnowledgeGraph'
          AND TABLE_NAME = ?
        """,
        table_name,
    ).fetchall()
    return table_name, {row[0] for row in column_rows}


def fetch_knowledge_nodes(
    conn: pyodbc.Connection,
    *,
    include_all_versions: bool,
    limit: int | None,
) -> list[KnowledgeNode]:
    where = "WHERE Name IS NOT NULL AND LTRIM(RTRIM(Name)) <> ''"
    if not include_all_versions:
        where += " AND (IsCurrent = 1 OR Status = 'Current')"

    top = f"TOP ({int(limit)}) " if limit else ""
    rows = conn.cursor().execute(
        f"""
        SELECT {top}Name, StableId
        FROM [KnowledgeGraph].[KnowledgeNodes]
        {where}
        ORDER BY Name
        """
    ).fetchall()

    seen: set[str] = set()
    nodes: list[KnowledgeNode] = []
    for row in rows:
        stable_id = str(row.StableId)
        if stable_id in seen:
            continue
        seen.add(stable_id)
        nodes.append(KnowledgeNode(name=str(row.Name).strip(), stable_id=stable_id))

    return nodes


def clean_url(url: str) -> str | None:
    url = unwrap_bing_url(url)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url.strip()


def unwrap_bing_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower().endswith("bing.com") and parsed.path.startswith("/ck/"):
        encoded_values = parse_qs(parsed.query).get("u")
        if encoded_values:
            encoded = encoded_values[0]
            if encoded.startswith("a1"):
                encoded = encoded[2:]
            try:
                padding = "=" * (-len(encoded) % 4)
                return base64.urlsafe_b64decode(encoded + padding).decode("utf-8", errors="replace")
            except Exception:
                return url

    return unquote(url)


def is_blocked_url(url: str, blocked_domains: set[str]) -> bool:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == domain or host.endswith(f".{domain}") for domain in blocked_domains)


def get_latin_tokens(node_name: str) -> list[str]:
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]{3,}", node_name)
        if not token.isdigit() and token.lower() not in TOKEN_STOPWORDS
    ]


def is_relevant(candidate: ResourceCandidate, node_name: str, blocked_domains: set[str]) -> bool:
    if is_blocked_url(candidate.link, blocked_domains):
        return False

    latin_tokens = [
        token
        for token in get_latin_tokens(node_name)
    ]
    if not latin_tokens:
        return True

    haystack = f"{candidate.name} {candidate.link}".lower()
    hits = sum(1 for token in set(latin_tokens) if token in haystack)
    required_hits = len(set(latin_tokens)) if len(set(latin_tokens)) <= 3 else 2
    return hits >= required_hits


def search_resources_ddgs(
    query: str,
    max_results: int,
    retries: int,
    delay_seconds: float,
    *,
    verify_tls: bool,
    log_failures: bool,
) -> list[ResourceCandidate]:
    for attempt in range(1, retries + 1):
        try:
            try:
                ddgs_context = DDGS(verify=verify_tls)
            except TypeError:
                ddgs_context = DDGS()

            with ddgs_context as ddgs:
                raw_results = ddgs.text(
                    query,
                    region="wt-wt",
                    safesearch="moderate",
                    max_results=max_results * 2,
                )

            candidates: list[ResourceCandidate] = []
            seen_links: set[str] = set()
            for result in raw_results or []:
                title = (result.get("title") or result.get("name") or "").strip()
                href = clean_url(result.get("href") or result.get("link") or result.get("url") or "")
                if not title or not href or href in seen_links:
                    continue
                seen_links.add(href)
                candidates.append(ResourceCandidate(name=title[:512], link=href))
                if len(candidates) >= max_results:
                    break

            return candidates
        except Exception as exc:
            message = str(exc)
            is_certificate_error = "cert verification failed" in message or "TLS handshake failed" in message
            if attempt == retries:
                if log_failures:
                    print(f"DDGS search failed for query '{query}': {exc}", file=sys.stderr)
                return []
            if is_certificate_error:
                if log_failures:
                    print(f"DDGS certificate error for query '{query}': {exc}", file=sys.stderr)
                return []
            time.sleep(delay_seconds * attempt)

    return []


def search_resources_bing(query: str, max_results: int) -> list[ResourceCandidate]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "https://www.bing.com/search",
        params={"q": query, "mkt": "en-US", "setlang": "en"},
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        },
        timeout=20,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[ResourceCandidate] = []
    seen_links: set[str] = set()
    for result in soup.select("li.b_algo h2 a"):
        title = result.get_text(" ", strip=True)
        href = clean_url(result.get("href") or "")
        if not title or not href or href in seen_links:
            continue
        seen_links.add(href)
        candidates.append(ResourceCandidate(name=title[:512], link=href))
        if len(candidates) >= max_results:
            break
    return candidates


def title_case_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip()).strip('"')


def wikipedia_slug(name: str) -> str:
    normalized = title_case_query(name)
    if normalized.lower().endswith(" varieties"):
        normalized = normalized[:-3] + "y"
    return quote(normalized.replace(" ", "_"), safe="_()-,")


def search_resources_curated(node_name: str, max_results: int) -> list[ResourceCandidate]:
    clean_name = title_case_query(node_name)
    encoded = quote(clean_name)
    candidates = [
        ResourceCandidate(
            name=f"{clean_name} - Wikipedia",
            link=f"https://en.wikipedia.org/wiki/{wikipedia_slug(clean_name)}",
        ),
        ResourceCandidate(
            name=f"{clean_name} - MIT OpenCourseWare search",
            link=f"https://ocw.mit.edu/search/?q={encoded}",
        ),
        ResourceCandidate(
            name=f"{clean_name} - Coursera search",
            link=f"https://www.coursera.org/search?query={encoded}",
        ),
        ResourceCandidate(
            name=f"{clean_name} - YouTube learning resources",
            link=f"https://www.youtube.com/results?search_query={encoded}%20lecture%20course",
        ),
    ]
    return candidates[:max_results]


def search_resources(
    query: str,
    node_name: str,
    max_results: int,
    retries: int,
    delay_seconds: float,
    provider: str,
    ddgs_verify_tls: bool,
) -> list[ResourceCandidate]:
    if provider in {"auto", "ddgs"}:
        results = search_resources_ddgs(
            query,
            max_results,
            retries,
            delay_seconds,
            verify_tls=ddgs_verify_tls,
            log_failures=provider == "ddgs",
        )
        if results or provider == "ddgs":
            return results

    if provider in {"auto", "bing"}:
        try:
            results = search_resources_bing(query, max_results)
            if results or provider == "bing":
                return results
        except Exception as exc:
            print(f"Bing search failed for query '{query}': {exc}", file=sys.stderr)

    if provider in {"auto", "curated"}:
        return search_resources_curated(node_name, max_results)

    return []


def ensure_resource(
    conn: pyodbc.Connection,
    table_name: str,
    columns: set[str],
    candidate: ResourceCandidate,
    *,
    dry_run: bool,
) -> StoredResource:
    existing = conn.cursor().execute(
        f"SELECT TOP (1) [Id], [Name], [Link] FROM [KnowledgeGraph].[{table_name}] WHERE [Link] = ?",
        candidate.link,
    ).fetchone()
    if existing:
        return StoredResource(id=str(existing.Id), name=str(existing.Name or candidate.name), link=str(existing.Link))

    resource_id = str(uuid.uuid4())
    if dry_run:
        return StoredResource(id=resource_id, name=candidate.name, link=candidate.link)

    insert_columns = ["Id", "Name", "Link"]
    values: list[object] = [resource_id, candidate.name, candidate.link]
    if "ReviewStatus" in columns:
        insert_columns.append("ReviewStatus")
        values.append(0)

    column_sql = ", ".join(f"[{column}]" for column in insert_columns)
    param_sql = ", ".join("?" for _ in insert_columns)
    conn.cursor().execute(
        f"INSERT INTO [KnowledgeGraph].[{table_name}] ({column_sql}) VALUES ({param_sql})",
        *values,
    )
    return StoredResource(id=resource_id, name=candidate.name, link=candidate.link)


def sync_neo4j_resource(
    driver,
    node: KnowledgeNode,
    resource: StoredResource,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        return

    with driver.session(default_access_mode="WRITE") as session:
        session.run(
            """
            MERGE (kn:KnowledgeNode {stableId: $stableId})
            SET kn.name = coalesce(kn.name, $nodeName)
            MERGE (r:Resource {id: $resourceId})
            SET r.uid = $resourceId,
                r.link = $link,
                r.url = $link,
                r.name = $resourceName
            MERGE (kn)-[:HAS_RESOURCE]->(r)
            """,
            stableId=node.stable_id,
            nodeName=node.name,
            resourceId=resource.id,
            link=resource.link,
            resourceName=resource.name,
        ).consume()


def build_query(node_name: str, language: str) -> str:
    ascii_ratio = sum(1 for char in node_name if ord(char) < 128) / max(len(node_name), 1)
    if language == "zh" and ascii_ratio > 0.8:
        language = "en"

    # if language == "zh":
    #     return f'"{node_name}" 学习资源 教程 课程'
    # if language == "en":
    #     return f'"{node_name}" tutorial course "lecture notes"'
    return f'"{node_name}"'


def process_nodes(args: argparse.Namespace) -> None:
    if args.env_file:
        load_dotenv(Path(args.env_file))
    appsettings = load_appsettings()

    sql_connection_string = args.sql_connection_string or get_config_value(
        appsettings,
        "SQL_CONNECTION_STRING",
        "ConnectionStrings",
        "DefaultConnection",
    )
    neo4j_uri = args.neo4j_uri or get_config_value(appsettings, "NEO4J_URI", "Neo4j", "Uri")
    neo4j_user = args.neo4j_user or get_config_value(appsettings, "NEO4J_USER", "Neo4j", "User")
    neo4j_password = args.neo4j_password or get_config_value(appsettings, "NEO4J_PASSWORD", "Neo4j", "Password")

    if not sql_connection_string:
        raise RuntimeError("SQL connection string is missing. Use --sql-connection-string or SQL_CONNECTION_STRING.")
    if not args.dry_run and (not neo4j_uri or not neo4j_user or not neo4j_password):
        raise RuntimeError("Neo4j config is missing. Use --neo4j-* args or NEO4J_* env vars.")

    sql_conn = open_sql_connection(sql_connection_string)
    neo4j_driver = None if args.dry_run else GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

    inserted_or_seen = 0
    relations = 0
    try:
        blocked_domains = set(DEFAULT_BLOCKED_DOMAINS)
        blocked_domains.update(domain.lower().strip() for domain in args.blocked_domain or [] if domain.strip())

        table_name, resource_columns = detect_resource_table(sql_conn)
        nodes = fetch_knowledge_nodes(
            sql_conn,
            include_all_versions=args.include_all_versions,
            limit=args.limit,
        )
        print(f"Loaded {len(nodes)} knowledge nodes. Resource table: KnowledgeGraph.{table_name}")

        for index, node in enumerate(nodes, start=1):
            query = build_query(node.name, args.language)
            print(f"[{index}/{len(nodes)}] Searching: {query}")
            raw_candidates = search_resources(
                query,
                node.name,
                max(args.max_results * 5, 10),
                args.retries,
                args.delay_seconds,
                args.search_provider,
                args.ddgs_verify_tls,
            )
            candidates = [
                candidate
                for candidate in raw_candidates
                if is_relevant(candidate, node.name, blocked_domains)
            ][: args.max_results]
            if not candidates and args.search_provider == "auto":
                candidates = [
                    candidate
                    for candidate in search_resources_curated(node.name, args.max_results)
                    if is_relevant(candidate, node.name, blocked_domains)
                ][: args.max_results]

            for candidate in candidates:
                stored = ensure_resource(sql_conn, table_name, resource_columns, candidate, dry_run=args.dry_run)
                inserted_or_seen += 1
                if neo4j_driver:
                    sync_neo4j_resource(neo4j_driver, node, stored, dry_run=args.dry_run)
                relations += 1
                print(f"  - {stored.name} | {stored.link} | {stored.id}")

            if not args.dry_run:
                sql_conn.commit()
            if args.node_delay_seconds > 0:
                time.sleep(args.node_delay_seconds)

        print(f"Done. Resources processed: {inserted_or_seen}. HAS_RESOURCE relations processed: {relations}.")
        if args.dry_run:
            print("Dry run only: no SQL inserts or Neo4j writes were made.")
    except Exception:
        sql_conn.rollback()
        raise
    finally:
        sql_conn.close()
        if neo4j_driver:
            neo4j_driver.close()


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover web resources for KnowledgeGraph.KnowledgeNodes and sync SQL + Neo4j.",
    )
    parser.add_argument("--sql-connection-string", help="SQL Server connection string. Defaults to appsettings.json or SQL_CONNECTION_STRING.")
    parser.add_argument("--neo4j-uri", help="Neo4j URI. Defaults to appsettings.json or NEO4J_URI.")
    parser.add_argument("--neo4j-user", help="Neo4j user. Defaults to appsettings.json or NEO4J_USER.")
    parser.add_argument("--neo4j-password", help="Neo4j password. Defaults to appsettings.json or NEO4J_PASSWORD.")
    parser.add_argument("--env-file", help="Optional .env file to load before reading environment variables.")
    parser.add_argument("--max-results", type=int, default=3, help="Maximum search results saved per knowledge node.")
    parser.add_argument("--limit", type=int, help="Only process the first N nodes.")
    parser.add_argument("--language", choices=["zh", "en", "mixed"], default="zh", help="Search query language.")
    parser.add_argument("--search-provider", choices=["auto", "ddgs", "bing", "curated"], default="auto", help="Search provider.")
    parser.add_argument("--ddgs-verify-tls", action="store_true", help="Enable DDGS TLS certificate verification. Off by default because some local proxy setups break it.")
    parser.add_argument("--blocked-domain", action="append", help="Domain to exclude from saved results. Can be repeated.")
    parser.add_argument("--retries", type=int, default=3, help="Search retry count per node.")
    parser.add_argument("--delay-seconds", type=float, default=3.0, help="Base search retry delay.")
    parser.add_argument("--node-delay-seconds", type=float, default=1.0, help="Delay between nodes to reduce search throttling.")
    parser.add_argument("--include-all-versions", action="store_true", help="Process every KnowledgeNodes row instead of only current rows.")
    parser.add_argument("--dry-run", action="store_true", help="Preview work without writing SQL or Neo4j.")
    return parser.parse_args(list(argv))


if __name__ == "__main__":
    process_nodes(parse_args(sys.argv[1:]))
