from services import bitrix, saby
from utils.session_manager import SessionManager


async def get_bitrix_service() -> bitrix.BitrixService:
    return bitrix.BitrixService(await SessionManager.get_session())


async def get_saby_service() -> saby.SabyService:
    return saby.SabyService(await SessionManager.get_session())
