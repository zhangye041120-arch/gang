from contextlib import contextmanager

from xiaoliao_agent.memory import PostgresMemoryRepository


class RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement, parameters=()):
        self.statements.append((" ".join(statement.split()), parameters))
        return self

    def fetchone(self):
        return (True, False)


def test_postgres_memory_repository_uses_unified_consent_table():
    connection = RecordingConnection()
    repository = object.__new__(PostgresMemoryRepository)

    @contextmanager
    def connect():
        yield connection

    repository._connect = connect
    repository.set_consent("user-1", True, False)
    assert repository.get_consent("user-1") == (True, False)

    sql = " ".join(statement for statement, _ in connection.statements)
    assert "ai_consents" in sql
    assert "ai_memory_consents" not in sql
