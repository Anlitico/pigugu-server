import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_register_user(client: AsyncClient):
    response = await client.post(
        "/v1/auth/register",
        json={"email": "test@example.com", "password": "password123", "display_name": "Test User"}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["display_name"] == "Test User"
    assert "id" in data

@pytest.mark.asyncio
async def test_register_user_duplicate_email(client: AsyncClient):
    # First registration
    await client.post(
        "/v1/auth/register",
        json={"email": "duplicate@example.com", "password": "password123"}
    )
    # Second registration with same email
    response = await client.post(
        "/v1/auth/register",
        json={"email": "duplicate@example.com", "password": "newpassword123"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Email already registered"

@pytest.mark.asyncio
async def test_login_success(client: AsyncClient):
    # Create user
    await client.post(
        "/v1/auth/register",
        json={"email": "login@example.com", "password": "password123"}
    )
    # Login
    response = await client.post(
        "/v1/auth/login",
        json={"email": "login@example.com", "password": "password123"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"

@pytest.mark.asyncio
async def test_login_invalid_credentials(client: AsyncClient):
    # Create user
    await client.post(
        "/v1/auth/register",
        json={"email": "wrongpass@example.com", "password": "password123"}
    )
    # Login with wrong password
    response = await client.post(
        "/v1/auth/login",
        json={"email": "wrongpass@example.com", "password": "wrongpassword"}
    )
    assert response.status_code == 401

@pytest.mark.asyncio
async def test_get_me(client: AsyncClient):
    # Register and login
    await client.post(
        "/v1/auth/register",
        json={"email": "me@example.com", "password": "password123"}
    )
    login_res = await client.post(
        "/v1/auth/login",
        json={"email": "me@example.com", "password": "password123"}
    )
    token = login_res.json()["access_token"]

    # Get Me
    response = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "me@example.com"

@pytest.mark.asyncio
async def test_refresh_token(client: AsyncClient, mock_redis):
    # Register and login
    await client.post(
        "/v1/auth/register",
        json={"email": "refresh@example.com", "password": "password123"}
    )
    login_res = await client.post(
        "/v1/auth/login",
        json={"email": "refresh@example.com", "password": "password123"}
    )
    refresh_token = login_res.json()["refresh_token"]

    # Refresh
    response = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": refresh_token}
    )
    assert response.status_code == 200
    assert "access_token" in response.json()

@pytest.mark.asyncio
async def test_logout(client: AsyncClient, mock_redis):
    # Register and login
    await client.post(
        "/v1/auth/register",
        json={"email": "logout@example.com", "password": "password123"}
    )
    login_res = await client.post(
        "/v1/auth/login",
        json={"email": "logout@example.com", "password": "password123"}
    )
    access_token = login_res.json()["access_token"]
    refresh_token = login_res.json()["refresh_token"]

    # Logout
    response = await client.post(
        "/v1/auth/logout",
        json={"refresh_token": refresh_token},
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 204
    # Verify mock_redis.delete was called
    assert mock_redis.delete.called

@pytest.mark.asyncio
async def test_change_password(client: AsyncClient, mock_redis):
    # Register and login
    await client.post(
        "/v1/auth/register",
        json={"email": "changepass@example.com", "password": "oldpassword"}
    )
    login_res = await client.post(
        "/v1/auth/login",
        json={"email": "changepass@example.com", "password": "oldpassword"}
    )
    token = login_res.json()["access_token"]

    # Change password
    response = await client.post(
        "/v1/auth/change-password",
        json={"old_password": "oldpassword", "new_password": "newpassword123"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 204

    # Try login with old password (should fail)
    login_res_old = await client.post(
        "/v1/auth/login",
        json={"email": "changepass@example.com", "password": "oldpassword"}
    )
    assert login_res_old.status_code == 401

    # Try login with new password (should succeed)
    login_res_new = await client.post(
        "/v1/auth/login",
        json={"email": "changepass@example.com", "password": "newpassword123"}
    )
    assert login_res_new.status_code == 200
