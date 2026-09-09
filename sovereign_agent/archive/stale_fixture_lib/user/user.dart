class User {
  final String id;
  final String username;
  DateTime lastLogin;

  User({
    required this.id,
    required this.username,
    required this.lastLogin,
  });

  /// Updates the user's last login timestamp to the current time.
  void updateLastLogin() {
    lastLogin = DateTime.now();
  }
}