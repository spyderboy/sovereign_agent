import 'package:flutter/foundation.dart';
import 'package:my_app/api/api_client.dart';

class UserProfile {
  final ApiClient apiClient;

  UserProfile(this.apiClient);

  /// Updates the profile picture of the user.
  Future<void> updateProfilePicture(String imageUrl) async {
    if (kDebugMode) {
      print('Updating profile picture to: $imageUrl');
    }
    // Simulation of uploading and updating the profile picture via the API client
    await Future.delayed(const Duration(milliseconds: 500)); // Simulate upload latency
    print('Profile picture updated successfully using URL: $imageUrl');
  }
}