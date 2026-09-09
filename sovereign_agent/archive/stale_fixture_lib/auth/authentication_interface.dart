abstract class IAuthentication {
  /// Authenticate the user.
  Future<bool> login();

  /// Logout the user and clear session data.
  Future<void> logout();

  /// Check if a valid authentication token or session exists.
  Future<bool> isAuthenticated();
}