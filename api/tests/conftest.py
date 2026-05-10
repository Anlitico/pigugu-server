import asyncio
import pytest
import bcrypt
from unittest.mock import AsyncMock, patch

# Fix bcrypt/passlib bug
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type("About", (), {"__version__": bcrypt.__version__})


@pytest.fixture(scope="session")
def event_loop():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
async def mock_redis():
    with patch("modules.auth.service.get_redis") as mocked_get_redis:
        mock_instance = AsyncMock()
        mock_instance.get.return_value = None
        mock_instance.set.return_value = True
        mock_instance.exists.return_value = True
        mock_instance.delete.return_value = True
        mock_instance.keys.return_value = []
        mocked_get_redis.return_value = mock_instance
        yield mock_instance


@pytest.fixture(autouse=True)
def mock_firebase():
    with patch("modules.push.service.init_firebase"):
        yield
