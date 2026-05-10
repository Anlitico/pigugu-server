# User Authentication — Flutter App Integration Spec

## 1. Dependencies

Add to `pubspec.yaml`:

```yaml
dependencies:
  dio: ^5.x                    # HTTP client
  flutter_secure_storage: ^9.x # Secure token persistence (iOS Keychain / Android Keystore)
  flutter_bloc: ^8.x           # State management (assumed already used)
  equatable: ^2.x              # Value equality for BLoC states
```

---

## 2. Token Storage (`flutter_secure_storage`)

### What it is
`flutter_secure_storage` is a Flutter plugin that stores key-value pairs inside the OS-level secure enclave:
- **iOS**: Apple Keychain Services
- **Android**: Android Keystore + EncryptedSharedPreferences

No server or external database is involved. Data lives on the device, persists across app restarts, and is automatically deleted if the app is uninstalled.

### Storage Keys

```dart
// lib/core/auth/token_storage.dart

class TokenStorage {
  static const _storage = FlutterSecureStorage();
  static const _accessTokenKey  = 'auth_access_token';
  static const _refreshTokenKey = 'auth_refresh_token';

  Future<void> saveTokens({
    required String accessToken,
    required String refreshToken,
  }) async {
    await _storage.write(key: _accessTokenKey,  value: accessToken);
    await _storage.write(key: _refreshTokenKey, value: refreshToken);
  }

  Future<String?> getAccessToken()  async => _storage.read(key: _accessTokenKey);
  Future<String?> getRefreshToken() async => _storage.read(key: _refreshTokenKey);

  Future<void> clearTokens() async {
    await _storage.delete(key: _accessTokenKey);
    await _storage.delete(key: _refreshTokenKey);
  }
}
```

---

## 3. API Client (`lib/data/remote/auth_api.dart`)

### Base Setup (Dio)

```dart
// lib/core/network/api_client.dart

class ApiClient {
  late final Dio dio;
  final TokenStorage _tokenStorage;

  ApiClient(this._tokenStorage) {
    dio = Dio(BaseOptions(
      baseUrl: AppConfig.baseUrl,        // e.g. https://api.pigugu.com/v1
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 10),
    ));

    dio.interceptors.add(AuthInterceptor(dio, _tokenStorage));
  }
}
```

### Auth Interceptor (Auto Token Refresh)

```dart
// lib/core/network/auth_interceptor.dart

class AuthInterceptor extends Interceptor {
  final Dio _dio;
  final TokenStorage _storage;
  bool _isRefreshing = false;

  AuthInterceptor(this._dio, this._storage);

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) async {
    final token = await _storage.getAccessToken();
    if (token != null) {
      options.headers['Authorization'] = 'Bearer $token';
    }
    handler.next(options);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) async {
    // Only retry once on 401, and not for the refresh endpoint itself
    if (err.response?.statusCode == 401 && !_isRefreshing) {
      _isRefreshing = true;
      try {
        final refreshToken = await _storage.getRefreshToken();
        if (refreshToken == null) {
          // No refresh token available → force logout
          await _storage.clearTokens();
          handler.next(err);
          return;
        }

        // Call refresh endpoint
        final response = await _dio.post('/auth/refresh', data: {
          'refresh_token': refreshToken,
        });

        final newAccess  = response.data['access_token'] as String;
        final newRefresh = response.data['refresh_token'] as String;
        await _storage.saveTokens(accessToken: newAccess, refreshToken: newRefresh);

        // Retry the original request with new token
        err.requestOptions.headers['Authorization'] = 'Bearer $newAccess';
        final retryResponse = await _dio.fetch(err.requestOptions);
        handler.resolve(retryResponse);
      } catch (_) {
        // Refresh also failed → clear tokens, user must re-login
        await _storage.clearTokens();
        handler.next(err);
      } finally {
        _isRefreshing = false;
      }
    } else {
      handler.next(err);
    }
  }
}
```

---

## 4. Auth API Methods (`lib/data/remote/auth_api.dart`)

```dart
class AuthApi {
  final Dio _dio;
  AuthApi(ApiClient client) : _dio = client.dio;

  /// Register a new account.
  /// Returns [UserDto] on success.
  /// Throws [DioException] with statusCode 409 if email exists.
  Future<UserDto> register({
    required String email,
    required String password,
    String? displayName,
  }) async {
    final response = await _dio.post('/auth/register', data: {
      'email': email,
      'password': password,
      if (displayName != null) 'display_name': displayName,
    });
    return UserDto.fromJson(response.data);
  }

  /// Login with email and password.
  /// Returns [TokenDto] containing access and refresh tokens.
  /// Throws [DioException] with statusCode 401 on wrong credentials.
  Future<TokenDto> login({
    required String email,
    required String password,
  }) async {
    final response = await _dio.post('/auth/login', data: {
      'email': email,
      'password': password,
    });
    return TokenDto.fromJson(response.data);
  }

  /// Logout: revokes the refresh token on the server.
  Future<void> logout({required String refreshToken}) async {
    await _dio.post('/auth/logout', data: {'refresh_token': refreshToken});
  }

  /// Change password for the currently authenticated user.
  /// Throws [DioException] with statusCode 400 if old password is wrong.
  Future<void> changePassword({
    required String oldPassword,
    required String newPassword,
  }) async {
    await _dio.post('/auth/change-password', data: {
      'old_password': oldPassword,
      'new_password': newPassword,
    });
  }
}
```

---

## 5. Data Transfer Objects (DTOs)

```dart
// lib/data/models/auth_dto.dart

class UserDto {
  final String id;
  final String email;
  final String? displayName;

  const UserDto({required this.id, required this.email, this.displayName});

  factory UserDto.fromJson(Map<String, dynamic> json) => UserDto(
    id:          json['id'] as String,
    email:       json['email'] as String,
    displayName: json['display_name'] as String?,
  );
}

class TokenDto {
  final String accessToken;
  final String refreshToken;

  const TokenDto({required this.accessToken, required this.refreshToken});

  factory TokenDto.fromJson(Map<String, dynamic> json) => TokenDto(
    accessToken:  json['access_token'] as String,
    refreshToken: json['refresh_token'] as String,
  );
}
```

---

## 6. Auth Repository (`lib/features/auth/auth_repository.dart`)

The repository is the single source of truth for auth state. BLoC talks only to the repository, never directly to the API.

```dart
class AuthRepository {
  final AuthApi _api;
  final TokenStorage _storage;

  AuthRepository(this._api, this._storage);

  /// Register and automatically persist tokens (login right after register).
  Future<UserDto> register({
    required String email,
    required String password,
    String? displayName,
  }) async {
    // Register → get user back
    final user = await _api.register(
      email: email, password: password, displayName: displayName,
    );
    // Auto-login after register
    final tokens = await _api.login(email: email, password: password);
    await _storage.saveTokens(
      accessToken: tokens.accessToken,
      refreshToken: tokens.refreshToken,
    );
    return user;
  }

  Future<UserDto> login({required String email, required String password}) async {
    final tokens = await _api.login(email: email, password: password);
    await _storage.saveTokens(
      accessToken: tokens.accessToken,
      refreshToken: tokens.refreshToken,
    );
    // Decode user id from access token (or call /me endpoint if available)
    // Minimal approach: return a UserDto constructed from stored data
    // TODO: add GET /v1/auth/me endpoint for full profile
    return _decodeUserFromToken(tokens.accessToken);
  }

  Future<void> logout() async {
    final refreshToken = await _storage.getRefreshToken();
    if (refreshToken != null) {
      try {
        await _api.logout(refreshToken: refreshToken);
      } catch (_) {
        // Best-effort server revocation; always clear local tokens
      }
    }
    await _storage.clearTokens();
  }

  Future<void> changePassword({
    required String oldPassword,
    required String newPassword,
  }) async {
    await _api.changePassword(oldPassword: oldPassword, newPassword: newPassword);
    // All server sessions revoked; clear local tokens too
    await _storage.clearTokens();
  }

  /// Check if a valid token exists on device startup.
  Future<bool> isLoggedIn() async {
    final token = await _storage.getAccessToken();
    return token != null;
  }
}
```

> [!NOTE]
> A `GET /v1/auth/me` endpoint should be added to the server (separate task)
> to return the full user profile. The `login()` method above needs this to
> return a complete `UserDto`. As a short-term workaround, decode the `sub`
> claim from the JWT to get the `user_id`.

---

## 7. Auth BLoC

### States (`auth_state.dart`)

```dart
sealed class AuthState extends Equatable {
  @override
  List<Object?> get props => [];
}

/// Initial state before app checks stored token
class AuthInitial extends AuthState {}

/// Token found in storage, user is logged in
class AuthAuthenticated extends AuthState {
  final UserDto user;
  AuthAuthenticated(this.user);
  @override List<Object?> get props => [user];
}

/// No token in storage, must log in
class AuthUnauthenticated extends AuthState {}

/// Loading (during login / register / logout)
class AuthLoading extends AuthState {}

/// An error occurred (wrong password, network error, etc.)
class AuthError extends AuthState {
  final String message;
  AuthError(this.message);
  @override List<Object?> get props => [message];
}
```

### Events (`auth_event.dart`)

```dart
sealed class AuthEvent extends Equatable {
  @override List<Object?> get props => [];
}

class AuthStarted extends AuthEvent {}  // App startup: check stored token

class AuthLoginRequested extends AuthEvent {
  final String email;
  final String password;
  AuthLoginRequested(this.email, this.password);
}

class AuthRegisterRequested extends AuthEvent {
  final String email;
  final String password;
  final String? displayName;
  AuthRegisterRequested(this.email, this.password, this.displayName);
}

class AuthLogoutRequested extends AuthEvent {}

class AuthChangePasswordRequested extends AuthEvent {
  final String oldPassword;
  final String newPassword;
  AuthChangePasswordRequested(this.oldPassword, this.newPassword);
}
```

### BLoC (`auth_bloc.dart`)

```dart
class AuthBloc extends Bloc<AuthEvent, AuthState> {
  final AuthRepository _repo;

  AuthBloc(this._repo) : super(AuthInitial()) {
    on<AuthStarted>(_onStarted);
    on<AuthLoginRequested>(_onLogin);
    on<AuthRegisterRequested>(_onRegister);
    on<AuthLogoutRequested>(_onLogout);
    on<AuthChangePasswordRequested>(_onChangePassword);
  }

  Future<void> _onStarted(AuthStarted event, Emitter<AuthState> emit) async {
    final loggedIn = await _repo.isLoggedIn();
    if (loggedIn) {
      // TODO: fetch user profile via GET /me
      emit(AuthAuthenticated(/* user */));
    } else {
      emit(AuthUnauthenticated());
    }
  }

  Future<void> _onLogin(AuthLoginRequested event, Emitter<AuthState> emit) async {
    emit(AuthLoading());
    try {
      final user = await _repo.login(email: event.email, password: event.password);
      emit(AuthAuthenticated(user));
    } on DioException catch (e) {
      final msg = e.response?.statusCode == 401
          ? 'Incorrect email or password'
          : 'Network error, please try again';
      emit(AuthError(msg));
    }
  }

  Future<void> _onRegister(AuthRegisterRequested event, Emitter<AuthState> emit) async {
    emit(AuthLoading());
    try {
      final user = await _repo.register(
        email: event.email,
        password: event.password,
        displayName: event.displayName,
      );
      emit(AuthAuthenticated(user));
    } on DioException catch (e) {
      final msg = e.response?.statusCode == 409
          ? 'This email is already registered'
          : 'Registration failed, please try again';
      emit(AuthError(msg));
    }
  }

  Future<void> _onLogout(AuthLogoutRequested event, Emitter<AuthState> emit) async {
    emit(AuthLoading());
    await _repo.logout();
    emit(AuthUnauthenticated());
  }

  Future<void> _onChangePassword(
    AuthChangePasswordRequested event,
    Emitter<AuthState> emit,
  ) async {
    emit(AuthLoading());
    try {
      await _repo.changePassword(
        oldPassword: event.oldPassword,
        newPassword: event.newPassword,
      );
      emit(AuthUnauthenticated()); // All sessions revoked, must re-login
    } on DioException catch (e) {
      final msg = e.response?.statusCode == 400
          ? 'Current password is incorrect'
          : 'Failed to change password';
      emit(AuthError(msg));
    }
  }
}
```

---

## 8. UI Screens

### 8.1 Login Screen

**Route**: `/login`

**Elements**:
| Element        | ID                     | Notes                              |
| -------------- | ---------------------- | ---------------------------------- |
| Email field    | `login_email_field`    | Keyboard: email, auto-lowercase    |
| Password field | `login_password_field` | Obscured, toggle visibility button |
| Login button   | `login_submit_button`  | Disabled while loading             |
| Register link  | `login_register_link`  | Navigates to `/register`           |
| Error banner   | `login_error_banner`   | Shown when `AuthError` state       |

**Behavior**:
- Dispatch `AuthLoginRequested` on submit
- Show `CircularProgressIndicator` on `AuthLoading`
- On `AuthAuthenticated` → navigate to home (clear back stack)
- On `AuthError` → show `SnackBar` or inline error text

---

### 8.2 Register Screen

**Route**: `/register`

**Elements**:
| Element            | ID                        | Notes                                        |
| ------------------ | ------------------------- | -------------------------------------------- |
| Display name field | `register_name_field`     | Optional, placeholder "Your name (optional)" |
| Email field        | `register_email_field`    | Keyboard: email                              |
| Password field     | `register_password_field` | Min 8 chars, show strength indicator         |
| Register button    | `register_submit_button`  |                                              |
| Login link         | `register_login_link`     | Back to `/login`                             |

**Behavior**:
- Dispatch `AuthRegisterRequested` on submit
- Client-side validation: password ≥ 8 chars before sending
- On `AuthAuthenticated` → navigate to home (clear back stack)
- On 409 error → "This email is already registered"

---

### 8.3 Change Password Screen

**Route**: `/settings/change-password`

**Access**: Only visible when `AuthAuthenticated`

**Elements**:
| Element                | ID                        | Notes                 |
| ---------------------- | ------------------------- | --------------------- |
| Current password field | `change_pw_old_field`     | Obscured              |
| New password field     | `change_pw_new_field`     | Obscured, min 8 chars |
| Save button            | `change_pw_submit_button` |                       |

**Behavior**:
- Dispatch `AuthChangePasswordRequested` on submit
- On success (→ `AuthUnauthenticated`) → navigate to `/login` with message "Password changed. Please log in again."
- On 400 error → "Current password is incorrect"

---

## 9. Navigation Guard

Use `BlocListener` at the root of the app to react to auth state changes:

```dart
// lib/app.dart

BlocListener<AuthBloc, AuthState>(
  listener: (context, state) {
    if (state is AuthUnauthenticated) {
      // Push login and clear the entire back stack
      context.go('/login');
    } else if (state is AuthAuthenticated) {
      context.go('/home');
    }
  },
  child: RouterWidget(),
)
```

On app startup, dispatch `AuthStarted` in `main.dart` after the BLoC is provided:
```dart
context.read<AuthBloc>().add(AuthStarted());
```

---

## 10. File Structure Summary

```
lib/
├── core/
│   ├── auth/
│   │   └── token_storage.dart
│   └── network/
│       ├── api_client.dart
│       └── auth_interceptor.dart
├── data/
│   ├── models/
│   │   └── auth_dto.dart
│   └── remote/
│       └── auth_api.dart
└── features/
    └── auth/
        ├── auth_repository.dart
        ├── bloc/
        │   ├── auth_bloc.dart
        │   ├── auth_event.dart
        │   └── auth_state.dart
        └── screens/
            ├── login_screen.dart
            ├── register_screen.dart
            └── change_password_screen.dart
```

---

## 11. Pending Server Endpoint

The app needs a `GET /v1/auth/me` endpoint to fetch the authenticated user's profile (id, email, display_name) after token refresh or app restart. This should be added to the server alongside the auth endpoints.

```
GET /v1/auth/me
Authorization: Bearer <access_token>

Response 200:
{
  "id": "...",
  "email": "jane@example.com",
  "display_name": "Jane"
}
```
