# connectors/asyncio.py
# Copyright (C) 2005-2024 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""generic asyncio-adapted versions of DBAPI connection and cursor"""

from __future__ import annotations

import asyncio
import collections
import itertools
import sys
from typing import Any
from typing import Deque
from typing import Iterator
from typing import NoReturn
from typing import Optional
from typing import Protocol
from typing import Sequence

from ..engine import AdaptedConnection
from ..engine.interfaces import _DBAPICursorDescription
from ..engine.interfaces import _DBAPIMultiExecuteParams
from ..engine.interfaces import _DBAPISingleExecuteParams
from ..util.concurrency import await_
from ..util.typing import Self


class AsyncIODBAPIConnection(Protocol):
    """protocol representing an async adapted version of a
    :pep:`249` database connection.


    """

    async def close(self) -> None:
        ...

    async def commit(self) -> None:
        ...

    def cursor(self) -> AsyncIODBAPICursor:
        ...

    async def rollback(self) -> None:
        ...


class AsyncIODBAPICursor(Protocol):
    """protocol representing an async adapted version
    of a :pep:`249` database cursor.


    """

    def __aenter__(self) -> Any:
        ...

    @property
    def description(
        self,
    ) -> _DBAPICursorDescription:
        """The description attribute of the Cursor."""
        ...

    @property
    def rowcount(self) -> int:
        ...

    arraysize: int

    lastrowid: int

    async def close(self) -> None:
        ...

    async def execute(
        self,
        operation: Any,
        parameters: Optional[_DBAPISingleExecuteParams] = None,
    ) -> Any:
        ...

    async def executemany(
        self,
        operation: Any,
        parameters: _DBAPIMultiExecuteParams,
    ) -> Any:
        ...

    async def fetchone(self) -> Optional[Any]:
        ...

    async def fetchmany(self, size: Optional[int] = ...) -> Sequence[Any]:
        ...

    async def fetchall(self) -> Sequence[Any]:
        ...

    async def setinputsizes(self, sizes: Sequence[Any]) -> None:
        ...

    def setoutputsize(self, size: Any, column: Any) -> None:
        ...

    async def callproc(
        self, procname: str, parameters: Sequence[Any] = ...
    ) -> Any:
        ...

    async def nextset(self) -> Optional[bool]:
        ...


class AsyncAdapt_dbapi_cursor:
    server_side = False
    __slots__ = (
        "_adapt_connection",
        "_connection",
        "_cursor",
        "_rows",
    )

    _cursor: AsyncIODBAPICursor
    _adapt_connection: AsyncAdapt_dbapi_connection
    _connection: AsyncIODBAPIConnection
    _rows: Deque[Any]

    def __init__(self, adapt_connection: AsyncAdapt_dbapi_connection):
        self._adapt_connection = adapt_connection
        self._connection = adapt_connection._connection

        cursor = self._make_new_cursor(self._connection)

        try:
            self._cursor = await_(cursor.__aenter__())
        except Exception as error:
            self._adapt_connection._handle_exception(error)

        self._rows = collections.deque()

    def _make_new_cursor(
        self, connection: AsyncIODBAPIConnection
    ) -> AsyncIODBAPICursor:
        return connection.cursor()

    @property
    def description(self) -> Optional[_DBAPICursorDescription]:
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def arraysize(self) -> int:
        return self._cursor.arraysize

    @arraysize.setter
    def arraysize(self, value: int) -> None:
        self._cursor.arraysize = value

    @property
    def lastrowid(self) -> int:
        return self._cursor.lastrowid

    def close(self) -> None:
        # note we aren't actually closing the cursor here,
        # we are just letting GC do it.  see notes in aiomysql dialect
        self._rows.clear()

    def execute(
        self,
        operation: Any,
        parameters: Optional[_DBAPISingleExecuteParams] = None,
    ) -> Any:
        try:
            return await_(self._execute_async(operation, parameters))
        except Exception as error:
            self._adapt_connection._handle_exception(error)

    def executemany(
        self,
        operation: Any,
        seq_of_parameters: _DBAPIMultiExecuteParams,
    ) -> Any:
        try:
            return await_(
                self._executemany_async(operation, seq_of_parameters)
            )
        except Exception as error:
            self._adapt_connection._handle_exception(error)

    async def _execute_async(
        self, operation: Any, parameters: Optional[_DBAPISingleExecuteParams]
    ) -> Any:
        async with self._adapt_connection._execute_mutex:
            if parameters is None:
                result = await self._cursor.execute(operation)
            else:
                result = await self._cursor.execute(operation, parameters)

            if self._cursor.description and not self.server_side:
                # aioodbc has a "fake" async result, so we have to pull it out
                # of that here since our default result is not async.
                # we could just as easily grab "_rows" here and be done with it
                # but this is safer.
                self._rows = collections.deque(await self._cursor.fetchall())
            return result

    async def _executemany_async(
        self,
        operation: Any,
        seq_of_parameters: _DBAPIMultiExecuteParams,
    ) -> Any:
        async with self._adapt_connection._execute_mutex:
            return await self._cursor.executemany(operation, seq_of_parameters)

    def nextset(self) -> None:
        await_(self._cursor.nextset())
        if self._cursor.description and not self.server_side:
            self._rows = collections.deque(await_(self._cursor.fetchall()))

    def setinputsizes(self, *inputsizes: Any) -> None:
        # NOTE: this is overrridden in aioodbc due to
        # see https://github.com/aio-libs/aioodbc/issues/451
        # right now

        return await_(self._cursor.setinputsizes(*inputsizes))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, type_: Any, value: Any, traceback: Any) -> None:
        self.close()

    def __iter__(self) -> Iterator[Any]:
        while self._rows:
            yield self._rows.popleft()

    def fetchone(self) -> Optional[Any]:
        if self._rows:
            return self._rows.popleft()
        else:
            return None

    def fetchmany(self, size: Optional[int] = None) -> Sequence[Any]:
        if size is None:
            size = self.arraysize

        rr = iter(self._rows)
        retval = list(itertools.islice(rr, 0, size))
        self._rows = collections.deque(rr)
        return retval

    def fetchall(self) -> Sequence[Any]:
        retval = list(self._rows)
        self._rows.clear()
        return retval


class AsyncAdapt_dbapi_ss_cursor(AsyncAdapt_dbapi_cursor):
    __slots__ = ()
    server_side = True

    def close(self) -> None:
        if self._cursor is not None:
            await_(self._cursor.close())
            self._cursor = None  # type: ignore

    def fetchone(self) -> Optional[Any]:
        return await_(self._cursor.fetchone())

    def fetchmany(self, size: Optional[int] = None) -> Any:
        return await_(self._cursor.fetchmany(size=size))

    def fetchall(self) -> Sequence[Any]:
        return await_(self._cursor.fetchall())


class AsyncAdapt_dbapi_connection(AdaptedConnection):
    _cursor_cls = AsyncAdapt_dbapi_cursor
    _ss_cursor_cls = AsyncAdapt_dbapi_ss_cursor

    __slots__ = ("dbapi", "_execute_mutex")

    _connection: AsyncIODBAPIConnection

    def __init__(self, dbapi: Any, connection: AsyncIODBAPIConnection):
        self.dbapi = dbapi
        self._connection = connection
        self._execute_mutex = asyncio.Lock()

    def cursor(self, server_side: bool = False) -> AsyncAdapt_dbapi_cursor:
        if server_side:
            return self._ss_cursor_cls(self)
        else:
            return self._cursor_cls(self)

    def execute(
        self,
        operation: Any,
        parameters: Optional[_DBAPISingleExecuteParams] = None,
    ) -> Any:
        """lots of DBAPIs seem to provide this, so include it"""
        cursor = self.cursor()
        cursor.execute(operation, parameters)
        return cursor

    def _handle_exception(self, error: Exception) -> NoReturn:
        exc_info = sys.exc_info()

        raise error.with_traceback(exc_info[2])

    def rollback(self) -> None:
        try:
            await_(self._connection.rollback())
        except Exception as error:
            self._handle_exception(error)

    def commit(self) -> None:
        try:
            await_(self._connection.commit())
        except Exception as error:
            self._handle_exception(error)

    def close(self) -> None:
        await_(self._connection.close())
