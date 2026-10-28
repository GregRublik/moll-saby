from aiohttp import ClientSession, TCPConnector


class SessionManager:
    _session: ClientSession | None = None

    @classmethod
    async def get_session(cls) -> ClientSession:
        """Возвращает сессию aiohttp, создавая её при первом вызове."""
        if cls._session is None or cls._session.closed:
            connector = TCPConnector(ttl_dns_cache=30)  # кеш DNS на 30 секунд
            cls._session = ClientSession(connector=connector)
        return cls._session

    @classmethod
    async def close_session(cls):
        """Закрывает сессию, если она существует."""
        if cls._session is not None:
            await cls._session.close()
            cls._session = None
