# Resource Discovery Agent

This agent reads `KnowledgeGraph.KnowledgeNodes`, searches web resources for each node name, inserts resources into `KnowledgeGraph.Resources` or `KnowledgeGraph.Resource`, and creates Neo4j `(:KnowledgeNode)-[:HAS_RESOURCE]->(:Resource)` relationships.

Run a safe preview first:

```powershell
python backend_python_part\agents\resource_discovery_agent.py --dry-run --limit 5 --max-results 3
```

If you are already inside `backend_python_part`, use:

```powershell
python agents\resource_discovery_agent.py --dry-run --limit 5 --max-results 3
```

Run writes:

```powershell
python backend_python_part\agents\resource_discovery_agent.py --limit 20 --max-results 3
```

Configuration is loaded from these places, in order:

- CLI args: `--sql-connection-string`, `--neo4j-uri`, `--neo4j-user`, `--neo4j-password`
- Environment variables: `SQL_CONNECTION_STRING`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`
- `SciencetopiaWebApplication/appsettings.json`

Use `--env-file .env` only when you want the agent to load local environment variables such as proxy settings. The repository `.env` contains Docker-oriented proxy values, so it is not loaded automatically.

Install dependencies if they are missing:

```powershell
pip install pyodbc neo4j ddgs
```

Notes:

- By default, only current knowledge-node rows are processed: `IsCurrent = 1 OR Status = 'Current'`.
- Use `--include-all-versions` if you intentionally want every versioned row.
- If the resource table has a `ReviewStatus` column, the agent writes `0`; if the column does not exist, it skips that column.
- Resources are deduplicated by `Link` before insertion.
- Search defaults to `--search-provider auto`: DDGS first, Bing second, then a curated fallback that produces stable learning-site URLs such as Wikipedia, MIT OCW, Coursera, and YouTube search pages. DDGS TLS verification is disabled by default because some Windows/proxy environments fail DuckDuckGo certificate validation. Use `--ddgs-verify-tls` if your network has a normal CA chain.
- Use `--search-provider curated` when search engines are blocked or return low-quality localized results.
- Common low-signal Q&A domains are blocked by default. Add more with repeated `--blocked-domain example.com`.
