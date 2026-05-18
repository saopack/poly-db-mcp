import re
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from .config_manager import ConfigManager
from .docker_manager import DockerManager
from .adapters import ADAPTER_REGISTRY
from .exceptions import (
    MCPError,
    DatabaseNotFoundError,
    AdapterError,
    AdapterConnectionError,
    AdapterExecutionError,
    DockerError,
)

logger = logging.getLogger(__name__)


_UNICODE_WHITESPACE = {
    ' ',  # non-breaking space
    ' ',  # en quad
    ' ',  # em quad
    ' ',  # en space
    ' ',  # em space
    ' ',  # three-per-em space
    ' ',  # four-per-em space
    ' ',  # six-per-em space
    ' ',  # figure space
    ' ',  # punctuation space
    ' ',  # thin space
    ' ',  # hair space
    ' ',  # narrow non-breaking space
    '　',  # ideographic space (CJK)
}


def _normalize_whitespace(query: str) -> str:
    """Replace Unicode whitespace characters with ASCII space.

    Oracle and other databases don't recognize Unicode whitespace as valid
    token separators, leading to ORA-06550 / PLS-00103 errors when SQL is
    copy-pasted from web pages, IDEs, or Chinese input methods.
    """
    result = []
    for ch in query:
        result.append(' ' if ch in _UNICODE_WHITESPACE else ch)
    return ''.join(result)


def _is_plsql_block(query: str) -> bool:
    """Check if query is a PL/SQL block that shouldn't be split on semicolons.

    Covers:
    - Oracle anonymous blocks: DECLARE ... BEGIN ... END; or BEGIN ... END;
    - CREATE FUNCTION / PROCEDURE / PACKAGE / TYPE / TRIGGER (any DB)
    - PostgreSQL/Vastbase functions are already protected by dollar-quoting,
      but this catches edge cases without dollar quotes.

    Excludes:
    - Bare BEGIN / BEGIN TRANSACTION / BEGIN WORK (PostgreSQL transaction start)
    """
    upper = query.strip().upper()
    if upper.startswith("DECLARE"):
        return True
    if upper.startswith("CREATE") and any(
        kw in upper for kw in (
            "FUNCTION ", "PROCEDURE ", "PACKAGE ", "TYPE ", "TRIGGER ",
            "OR REPLACE FUNCTION", "OR REPLACE PROCEDURE",
            "OR REPLACE PACKAGE", "OR REPLACE TYPE", "OR REPLACE TRIGGER",
        )
    ):
        return True
    # BEGIN with non-empty body that isn't TRANSACTION/WORK → PL/SQL block
    if upper.startswith("BEGIN"):
        after_begin = upper[5:].strip()
        if not after_begin:
            return False  # bare "BEGIN" or "BEGIN;"
        if after_begin.startswith("TRANSACTION") or after_begin.startswith("WORK"):
            return False
        return True
    return False


def _split_sql_statements(query: str) -> List[str]:
    """Split SQL string into individual statements.

    Handles semicolons inside:
    - Single-quoted string literals (including E'...' escape strings)
    - Double-quoted identifiers
    - Dollar-quoted strings (PostgreSQL): $$...$$ or $tag$...$tag$
    - Backtick-quoted identifiers (MySQL): `...`
    - Single-line comments (--)
    - Block comments (/* */)

    PL/SQL blocks (DECLARE/BEGIN...END, CREATE FUNCTION/PROCEDURE/...)
    are returned as a single statement — semicolons inside them are preserved.
    """
    statements = []
    current = []
    i = 0
    n = len(query)

    while i < n:
        ch = query[i]

        # Single-line comment: -- ... until end of line
        if ch == '-' and i + 1 < n and query[i + 1] == '-':
            current.append(ch)
            current.append(query[i + 1])
            i += 2
            while i < n and query[i] != '\n':
                current.append(query[i])
                i += 1
            continue

        # Block comment: /* ... */
        if ch == '/' and i + 1 < n and query[i + 1] == '*':
            current.append(ch)
            current.append(query[i + 1])
            i += 2
            while i + 1 < n and not (query[i] == '*' and query[i + 1] == '/'):
                current.append(query[i])
                i += 1
            if i + 1 < n:
                current.append(query[i])
                current.append(query[i + 1])
                i += 2
            continue

        # Dollar-quoted string (PostgreSQL): $$...$$ or $tag$...$tag$
        if ch == '$':
            # Check if this is a dollar-quote start, not a numeric literal like $5
            start = i
            i += 1
            tag_chars = []
            while i < n and query[i] != '$':
                tag_chars.append(query[i])
                i += 1
            if i < n:
                # Found closing $ of the opening tag
                tag = ''.join(tag_chars)
                # Append the full opening tag
                current.append('$')
                current.extend(tag_chars)
                current.append('$')
                i += 1
                # Look for closing tag: $tag$
                closing = f"${tag}$"
                clen = len(closing)
                while i + clen <= n:
                    if query[i:i + clen] == closing:
                        current.append(closing)
                        i += clen
                        break
                    current.append(query[i])
                    i += 1
                continue
            else:
                # Not a dollar-quote, just a lone $ at end of input
                i = start
                current.append(ch)
                i += 1
                continue

        # E'...' escape string (PostgreSQL)
        if ch in ('E', 'e') and i + 1 < n and query[i + 1] == "'":
            current.append(ch)
            current.append("'")
            i += 2
            while i < n:
                current.append(query[i])
                if query[i] == "'":
                    if i + 1 < n and query[i + 1] == "'":
                        current.append(query[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                if query[i] == '\\' and i + 1 < n:
                    current.append(query[i + 1])
                    i += 2
                    continue
                i += 1
            continue

        # Single-quoted string literal
        if ch == "'":
            current.append(ch)
            i += 1
            while i < n:
                current.append(query[i])
                if query[i] == "'":
                    if i + 1 < n and query[i + 1] == "'":
                        current.append(query[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Double-quoted identifier
        if ch == '"':
            current.append(ch)
            i += 1
            while i < n:
                current.append(query[i])
                if query[i] == '"':
                    if i + 1 < n and query[i + 1] == '"':
                        current.append(query[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Backtick-quoted identifier (MySQL)
        if ch == '`':
            current.append(ch)
            i += 1
            while i < n and query[i] != '`':
                current.append(query[i])
                i += 1
            if i < n:
                current.append(query[i])
                i += 1
            continue

        # Semicolon: statement separator (standard SQL delimiter)
        # Inside PL/SQL blocks, keep the semicolon as part of the statement
        if ch == ';':
            current_text = ''.join(current)
            if _is_plsql_block(current_text):
                current.append(ch)
                i += 1
                continue
            stmt = current_text.strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue

        if ch == '/':
            # / followed by newline or end-of-input: statement delimiter (Oracle)
            if i + 1 < n and query[i + 1] == '\r':
                stmt = ''.join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
                i += 2  # skip \r
                if i < n and query[i] == '\n':
                    i += 1  # skip \n
                continue
            if i + 1 < n and query[i + 1] == '\n':
                stmt = ''.join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
                i += 2
                continue
            if i + 1 >= n:
                stmt = ''.join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
                i += 1
                continue

        current.append(ch)
        i += 1

    stmt = ''.join(current).strip()
    if stmt:
        statements.append(stmt)

    return statements


class MCPExecutor:
    def __init__(self):
        self.docker_manager = DockerManager()

        self._ddl_pattern = re.compile(
            r'^\s*(CREATE|ALTER|DROP|TRUNCATE|RENAME)\s+',
            re.IGNORECASE
        )

    def _is_ddl_statement(self, query: str) -> bool:
        return bool(self._ddl_pattern.match(query.strip()))

    _REVERSE_DDL_PATTERNS = [
        # CREATE TABLE [IF NOT EXISTS] [schema.]name → DROP TABLE [schema.]name
        (
            re.compile(
                r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)',
                re.IGNORECASE,
            ),
            lambda m: f"DROP TABLE {m.group(1)}",
        ),
        # CREATE [UNIQUE] INDEX [IF NOT EXISTS] name → DROP INDEX name
        (
            re.compile(
                r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)',
                re.IGNORECASE,
            ),
            lambda m: f"DROP INDEX {m.group(1)}",
        ),
        # CREATE [OR REPLACE] VIEW name → DROP VIEW name
        (
            re.compile(
                r'CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\S+)',
                re.IGNORECASE,
            ),
            lambda m: f"DROP VIEW {m.group(1)}",
        ),
        # CREATE SEQUENCE [IF NOT EXISTS] name → DROP SEQUENCE name
        (
            re.compile(
                r'CREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)',
                re.IGNORECASE,
            ),
            lambda m: f"DROP SEQUENCE {m.group(1)}",
        ),
        # ALTER TABLE [IF EXISTS] [ONLY] t ADD [COLUMN] name type → ALTER TABLE t DROP COLUMN name
        (
            re.compile(
                r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(\S+)\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\S+)',
                re.IGNORECASE,
            ),
            lambda m: f"ALTER TABLE {m.group(1)} DROP COLUMN {m.group(2)}",
        ),
        # ALTER TABLE t ADD CONSTRAINT name → ALTER TABLE t DROP CONSTRAINT name
        (
            re.compile(
                r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(\S+)\s+ADD\s+CONSTRAINT\s+(\S+)',
                re.IGNORECASE,
            ),
            lambda m: f"ALTER TABLE {m.group(1)} DROP CONSTRAINT {m.group(2)}",
        ),
        # RENAME TABLE t1 TO t2 → RENAME TABLE t2 TO t1 (MySQL)
        (
            re.compile(
                r'RENAME\s+TABLE\s+(\S+)\s+TO\s+(\S+)',
                re.IGNORECASE,
            ),
            lambda m: f"RENAME TABLE {m.group(2)} TO {m.group(1)}",
        ),
        # ALTER TABLE t RENAME TO new_t → ALTER TABLE new_t RENAME TO t
        (
            re.compile(
                r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(\S+)\s+RENAME\s+TO\s+(\S+)',
                re.IGNORECASE,
            ),
            lambda m: f"ALTER TABLE {m.group(2)} RENAME TO {m.group(1)}",
        ),
    ]

    @staticmethod
    def _generate_reverse_ddl(stmt: str) -> str | None:
        """Generate a reverse DDL to undo the given DDL statement.

        Returns None for irreversible operations (DROP, TRUNCATE).
        """
        for pattern, builder in MCPExecutor._REVERSE_DDL_PATTERNS:
            m = pattern.search(stmt)
            if m:
                return builder(m)
        return None

    def _is_plsql_block(self, stmt: str) -> bool:
        """Delegate to module-level _is_plsql_block."""
        return _is_plsql_block(stmt)

    def _sanitize_statement(self, stmt: str) -> str:
        """Strip trailing semicolons and Oracle-style terminator.

        DB-API drivers (oracledb, pymysql, psycopg2, etc.) reject trailing `;`
        because it is a client-tool convention (SQL*Plus, mysql CLI), not part
        of the SQL language. PL/SQL blocks preserve their trailing `;` (part of
        END; syntax) but strip Oracle-style `/` terminator.
        """
        stmt = stmt.strip()
        if self._is_plsql_block(stmt):
            # Strip Oracle-style trailing terminator: optional whitespace/newlines + /
            import re
            stmt = re.sub(r'\s*/\s*$', '', stmt)
            return stmt
        return stmt.rstrip(';')

    def _execute_one(self, adapter, stmt: str) -> Dict[str, Any]:
        """Execute a single statement directly, with timing and logging.

        Unlike _execute_single, this does NOT handle transaction wrapping,
        rollback, or reverse DDL. The caller is responsible for cleanup.
        """
        stmt = self._sanitize_statement(stmt)
        stmt_preview = stmt[:200] + '...' if len(stmt) > 200 else stmt
        logger.info(f"Executing SQL: {stmt_preview}")
        start_time = datetime.now(timezone.utc)
        start_ts = time.time()
        try:
            result = adapter.execute(stmt)
            end_ts = time.time()
            elapsed = round((end_ts - start_ts) * 1000, 2)
            logger.info(f"SQL completed in {elapsed}ms, rows={result.get('row_count', 0)}")
        except Exception:
            end_ts = time.time()
            elapsed = round((end_ts - start_ts) * 1000, 2)
            logger.error(f"SQL failed after {elapsed}ms: {stmt_preview}")
            raise
        end_time = datetime.now(timezone.utc)
        return {
            "status": "success",
            "data": result,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "elapsed_ms": elapsed,
        }

    def _execute_single(self, adapter, stmt: str) -> Dict[str, Any]:
        """Execute a single statement.

        Routes to the appropriate execution path:
        - DDL on databases without DDL transaction support → direct execute (container destroyed)
        - All other cases → execute within transaction then rollback (container stays clean)
        """
        stmt = self._sanitize_statement(stmt)
        stmt_preview = stmt[:200] + '...' if len(stmt) > 200 else stmt
        logger.info(f"Executing SQL: {stmt_preview}")
        start_time = datetime.now(timezone.utc)
        start_ts = time.time()
        is_ddl = self._is_ddl_statement(stmt)

        # DDL on databases that don't support transactional DDL: execute directly.
        # Try reverse DDL to keep the container clean; destroy only as last resort.
        if is_ddl and not adapter.supports_ddl_transaction:
            try:
                result = adapter.execute(stmt)
            except Exception:
                end_ts = time.time()
                elapsed = round((end_ts - start_ts) * 1000, 2)
                logger.error(f"DDL failed after {elapsed}ms: {stmt_preview}")
                raise
            end_ts = time.time()
            elapsed = round((end_ts - start_ts) * 1000, 2)
            logger.info(f"DDL (direct) completed in {elapsed}ms")
            end_time = datetime.now(timezone.utc)

            reverse_stmt = self._generate_reverse_ddl(stmt)
            if reverse_stmt:
                try:
                    logger.info(f"Executing reverse DDL: {reverse_stmt}")
                    adapter.execute(reverse_stmt)
                    return {
                        "status": "success",
                        "data": result,
                        "start_time": start_time.isoformat(),
                        "end_time": end_time.isoformat(),
                        "elapsed_ms": elapsed,
                    }
                except Exception as exc:
                    logger.warning(f"Reverse DDL failed ({exc}), container will be destroyed")

            return {
                "status": "success",
                "data": result,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "elapsed_ms": elapsed,
                "note": "DDL executed on database that does not support transactional DDL, container will be destroyed",
            }

        # All other cases: wrap in transaction + rollback to keep data clean
        try:
            result = adapter.execute_with_rollback(stmt)
            end_ts = time.time()
            elapsed = round((end_ts - start_ts) * 1000, 2)
            logger.info(f"SQL completed in {elapsed}ms, rows={result.get('row_count', 0)}")
        except Exception:
            end_ts = time.time()
            elapsed = round((end_ts - start_ts) * 1000, 2)
            logger.error(f"SQL failed after {elapsed}ms: {stmt_preview}")
            raise
        end_time = datetime.now(timezone.utc)
        return {
            "status": "success",
            "data": result,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "elapsed_ms": elapsed,
        }

    def run_validation(
        self,
        db_type: str,
        version: str,
        query: str,
        db_compatibility: Optional[str] = None,
        explain: bool = False,
    ) -> Dict[str, Any]:
        if explain and not query.strip().upper().startswith("EXPLAIN"):
            query = f"EXPLAIN {query}"

        query = _normalize_whitespace(query)

        config = ConfigManager.get_db_config(db_type, version)
        if not config:
            return {"status": "error", "message": f"Unsupported database type or version: {db_type} {version}"}

        logger.info(f"run_validation: db_type={db_type}, version={version}, explain={explain}, "
                    f"query_preview={query[:200]}")

        # Vastbase 兼容性模式：运行时参数覆盖配置文件中的默认值
        if db_compatibility and db_type == 'vastbase':
            env = config.get('env', {})
            if env is not None:
                env = dict(env)
                env['VB_DBCOMPATIBILITY'] = db_compatibility
                config = dict(config)
                config['env'] = env

        container_id = None
        destroy_container = False

        try:
            container_name = None
            if db_compatibility and db_type == 'vastbase':
                container_name = f"db-mcp-{db_type}-{version}-{db_compatibility}"
            container_id, host_port = self.docker_manager.start_container(db_type, version, config, container_name)

            if not self.docker_manager.wait_for_port('localhost', host_port):
                return {"status": "error", "message": "Database container failed to start"}

            AdapterClass = ADAPTER_REGISTRY.get(db_type)
            if not AdapterClass:
                return {"status": "error", "message": f"No adapter found for {db_type}"}

            adapter = AdapterClass(config)

            try:
                # Retry connection — the port may be open before the DB is ready
                last_error = None
                for attempt in range(24):
                    try:
                        adapter.connect('localhost', host_port)
                        logger.info(f"Connected to {db_type} on port {host_port} (attempt {attempt + 1})")
                        break
                    except AdapterConnectionError as e:
                        last_error = e
                        if attempt < 23:
                            logger.debug(f"Connection attempt {attempt + 1} failed: {e}")
                            time.sleep(5)
                else:
                    logger.error(f"Failed to connect to {db_type} after 24 attempts: {last_error}")
                    return {"status": "error", "message": f"Database not ready after 120s: {last_error}"}

                statements = _split_sql_statements(query)
                logger.info(f"Split into {len(statements)} statement(s)")

                if not statements:
                    return {"status": "error", "message": "No SQL statements found"}

                if len(statements) == 1:
                    result = self._execute_single(adapter, statements[0])
                    if result.get("note"):
                        destroy_container = True
                    return result

                if adapter.supports_ddl_transaction:
                    # Wrap all statements in a single transaction so that later
                    # statements see the effects of earlier ones (e.g. CREATE
                    # TABLE then INSERT into it). Rollback at the end.
                    adapter.begin_transaction()
                    try:
                        results = []
                        for stmt in statements:
                            try:
                                entry = {"statement": stmt, **self._execute_one(adapter, stmt)}
                            except Exception as e:
                                entry = {"statement": stmt, "status": "error", "message": str(e)}
                            results.append(entry)
                            if entry["status"] == "error":
                                return {"status": "success", "data": results}
                        return {"status": "success", "data": results}
                    finally:
                        adapter.rollback()
                else:
                    # Non-transactional DDL: DDL auto-commits, DML runs in a
                    # shared transaction so later statements see earlier DML.
                    results = []
                    reverse_ddls = []
                    in_dml_txn = False

                    def _flush_dml():
                        nonlocal in_dml_txn
                        if in_dml_txn:
                            adapter.rollback()
                            in_dml_txn = False

                    try:
                        for stmt in statements:
                            if self._is_ddl_statement(stmt):
                                _flush_dml()
                                try:
                                    entry = {"statement": stmt, **self._execute_one(adapter, stmt)}
                                except Exception as e:
                                    entry = {"statement": stmt, "status": "error", "message": str(e)}
                                rev = self._generate_reverse_ddl(stmt)
                                if rev:
                                    reverse_ddls.append(rev)
                                else:
                                    entry["note"] = (
                                        "DDL executed on database that does not support "
                                        "transactional DDL, container will be destroyed"
                                    )
                                results.append(entry)
                                if entry["status"] == "error":
                                    break
                            else:
                                if not in_dml_txn:
                                    adapter.begin_transaction()
                                    in_dml_txn = True
                                try:
                                    entry = {"statement": stmt, **self._execute_one(adapter, stmt)}
                                except Exception as e:
                                    entry = {"statement": stmt, "status": "error", "message": str(e)}
                                results.append(entry)
                                if entry["status"] == "error":
                                    break
                    finally:
                        _flush_dml()

                    for rev in reversed(reverse_ddls):
                        try:
                            adapter.execute(rev)
                            logger.info(f"Reverse DDL executed: {rev}")
                        except Exception as exc:
                            logger.warning(
                                f"Reverse DDL failed ({exc}), container will be destroyed"
                            )
                            destroy_container = True

                    return {"status": "success", "data": results}
            finally:
                adapter.disconnect()

        except AdapterConnectionError as e:
            logger.error(f"Connection failed: {e}")
            return {"status": "error", "message": str(e)}
        except AdapterExecutionError as e:
            logger.error(f"Execution failed: {e}")
            return {"status": "error", "message": str(e)}
        except DockerError as e:
            logger.error(f"Docker error: {e}")
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.exception("Validation failed")
            return {"status": "error", "message": str(e)}
        finally:
            if container_id and destroy_container:
                self.docker_manager.stop_container(container_id)
