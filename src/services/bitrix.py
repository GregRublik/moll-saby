from aiohttp import ClientSession
from config import settings


class BitrixService:
    """Работа с api bitrix"""

    def __init__(self, http_session: ClientSession):
        self._session = http_session

    @staticmethod
    def _url(endpoint: str) -> str:
        return (
            f"{settings.bitrix.portal_url}/rest/{settings.bitrix.id_user}"
            f"/{settings.bitrix.key_823}/{endpoint}.json"
        )

    async def _get(self, endpoint: str, params: dict):
        response = await self._session.get(
            self._url(endpoint),
            params=params,
        )

        return await response.json()

    async def _post(self, endpoint: str, payload: dict):
        response = await self._session.post(
            self._url(endpoint),
            json=payload,
        )

        return await response.json()
