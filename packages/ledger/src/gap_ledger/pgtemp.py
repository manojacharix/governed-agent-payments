"""A throwaway Postgres cluster for tests and demos: initdb in a temp folder, TCP on localhost only.

Uses GAP_TEST_PG_DSN instead when set (e.g. a CI service). Never used in production.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg


def _bindir() -> Path:
    for candidate in [os.environ.get("GAP_PG_BINDIR"), shutil.which("initdb") and str(Path(shutil.which("initdb")).parent),
                      "/opt/homebrew/opt/postgresql@17/bin", *sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)]:
        if candidate and (Path(candidate) / "initdb").exists():
            return Path(candidate)
    raise RuntimeError("Postgres binaries not found: install Postgres or set GAP_PG_BINDIR / GAP_TEST_PG_DSN")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TempCluster:
    def __init__(self) -> None:
        self.dsn_base = os.environ.get("GAP_TEST_PG_DSN")
        self._dir = None
        if self.dsn_base:
            return
        bindir = _bindir()
        self._bindir = bindir
        self._dir = tempfile.mkdtemp(prefix="gap-pg-")
        data = Path(self._dir) / "data"
        try:
            subprocess.run([bindir / "initdb", "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8", "--no-locale"],
                           check=True, capture_output=True)
            self.port = _free_port()
            subprocess.run([bindir / "pg_ctl", "start", "-D", data, "-l", Path(self._dir) / "log", "-w", "-o",
                            f"-p {self.port} -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c fsync=off"],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise RuntimeError(f"temporary Postgres failed to start: {e.stderr.decode(errors='replace')[-500:]}") from None
        self.dsn_base = f"host=127.0.0.1 port={self.port} user=postgres"

    def dsn(self, dbname: str = "postgres") -> str:
        return f"{self.dsn_base} dbname={dbname}"

    def create_template(self, name: str, sql_files: list[Path]) -> None:
        with psycopg.connect(self.dsn(), autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')
            c.execute(f'CREATE DATABASE "{name}"')
        with psycopg.connect(self.dsn(name), autocommit=True) as c:
            for f in sql_files:
                c.execute(Path(f).read_text())

    def fresh_db(self, template: str, name: str) -> str:
        with psycopg.connect(self.dsn(), autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')
            c.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
        return self.dsn(name)

    def stop(self) -> None:
        if self._dir:
            subprocess.run([self._bindir / "pg_ctl", "-D", Path(self._dir) / "data", "-m", "immediate", "stop"], capture_output=True)
            shutil.rmtree(self._dir, ignore_errors=True)


LEDGER_SQL = sorted((Path(__file__).parent / "sql").glob("*.sql"))
