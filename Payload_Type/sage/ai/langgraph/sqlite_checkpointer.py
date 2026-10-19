"""SQLite checkpoint saver for nested HITL resumes.

LangGraph passes accumulated null-task writes back to the saver after each
resume. Their positional indexes can shift when a special resume write is
removed, so the SQLite rows must be replaced as one snapshot.
"""

from collections.abc import Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph._internal._constants import NULL_TASK_ID
from langgraph.checkpoint.base import WRITES_IDX_MAP
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class SageAsyncSqliteSaver(AsyncSqliteSaver):
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        if task_id != NULL_TASK_ID:
            await super().aput_writes(config, writes, task_id, task_path)
            return

        await self.setup()
        identity = config["configurable"]
        key = (
            str(identity["thread_id"]),
            str(identity["checkpoint_ns"]),
            str(identity["checkpoint_id"]),
            task_id,
        )
        async with self.lock, self.conn.cursor() as cursor:
            # Match the installed saver when a batch contains only special
            # channels: the final write to a special index wins.
            insert_mode = (
                "REPLACE" if all(channel in WRITES_IDX_MAP for channel, _ in writes)
                else "IGNORE"
            )
            rows = [
                (*key, WRITES_IDX_MAP.get(channel, index), channel,
                 *self.serde.dumps_typed(value))
                for index, (channel, value) in enumerate(writes)
            ]
            try:
                await cursor.execute(
                    "DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? "
                    "AND checkpoint_id=? AND task_id=?",
                    key,
                )
                await cursor.executemany(
                    f"INSERT OR {insert_mode} INTO writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
