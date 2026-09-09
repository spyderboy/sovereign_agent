import '../user/user.dart';
import '../database/database_interface.dart';

class UserRepository {
  final IDatabase idatabase;

  UserRepository(this.idatabase);

  /// Retrieves a user by their unique ID, handling null cases if the user does not exist.
  Future<User?> getUserById(String userId) async {
    // We assume IDatabase has a get method that returns raw data (e.g., Map).
    final rawData = await idatabase.get(userId);

    if (rawData == null) {
      return null; // User not found.
    }

    // Attempt to map the raw data into a User object, which is more robust than direct casting.
    if (rawData is Map<String, dynamic>) {
      try {
        final id = rawData['id'] as String?;
        final username = rawData['username'] as String?;
        // Assuming the database stores lastLogin as a format that can be cast to DateTime
        final lastLoginValue = rawData['lastLogin'];

        if (id != null && username != null && lastLoginValue != null) {
          // Type assertion for safety in context of database retrieval
          return User(
            id: id,
            username: username,
            lastLogin: lastLoginValue as DateTime,
          );
        }
      } catch (e) {
        print("Error mapping user data keys to User model for ID $userId: $e");
        return null;
      }
    }

    // Handle cases where rawData exists but is not a mappable structure.
    print("Error retrieving or casting user data for ID $userId: Data format unexpected.");
    return null;
  }
}