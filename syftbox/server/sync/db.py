import os
import shutil
import sqlite3
import tempfile

from loguru import logger

from syftbox.server.sync.models import FileMetadata


# @contextlib.contextmanager
def get_db(path: str):
    conn = sqlite3.connect(path)

    with conn:
        conn.execute("PRAGMA cache_size=10000;")
        conn.execute("PRAGMA synchronous=OFF;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        # Create the table if it doesn't exist
        conn.execute("""
        CREATE TABLE IF NOT EXISTS file_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            hash TEXT NOT NULL,
            signature TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            last_modified TEXT NOT NULL
        )
        """)
    return conn


def save_file_metadata(conn: sqlite3.Connection, metadata: FileMetadata):
    # Insert the metadata into the database or update if a conflict on 'path' occurs
    conn.execute(
        """
    INSERT INTO file_metadata (path, hash, signature, file_size, last_modified)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        hash = excluded.hash,
        signature = excluded.signature,
        file_size = excluded.file_size,
        last_modified = excluded.last_modified
    """,
        (
            str(metadata.path),
            metadata.hash,
            metadata.signature,
            metadata.file_size,
            metadata.last_modified.isoformat(),
        ),
    )


def get_all_metadata(
    conn: sqlite3.Connection, path_like: str | None = None
) -> list[FileMetadata]:
    query = "SELECT * FROM file_metadata"
    params = ()

    if path_like:
        query += " WHERE path LIKE ?"
        params = (path_like,)

    cursor = conn.execute(query, params)
    # would be nice to paginate
    return [
        FileMetadata(
            path=row[1],
            hash=row[2],
            signature=row[3],
            file_size=row[4],
            last_modified=row[5],
        )
        for row in cursor
    ]


def move_with_transaction(
    conn: sqlite3.Connection, *, origin_path: str, metadata: FileMetadata
):
    """The file system and database do not share transactions,
    so this operation is not atomic.
    Ideally, files (blobs) should be immutable,
    and the path should update to a new location
    whenever there is a change to the file contents.
    """

    # backup the original file
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_path = temp_file.name

    shutil.copy(origin_path, temp_path)

    cursor = conn.cursor()
    try:
        # Update database entry
        from_path = metadata.path
        metadata.path = origin_path
        save_file_metadata(conn, metadata)

        conn.commit()

        # WARNING: between the move and the commit
        # the database will be in an inconsistent state

        shutil.move(from_path, metadata.path)

    except sqlite3.IntegrityError:
        # Rollback the transaction in case of error
        conn.rollback()
        logger.error(f"Failed to update metadata for {metadata.path}. Rolled back.")

    finally:
        # Clean up
        cursor.close()
        conn.close()

        # Delete the temp file if it exists
        if os.path.exists(temp_path):
            os.remove(temp_path)
