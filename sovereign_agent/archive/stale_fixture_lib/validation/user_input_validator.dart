import 'package:flutter/foundation.dart';

class UserInputValidator {
  // Simple placeholder for other validation methods if they existed

  /// Validates an email address using a simple regex pattern.
  bool validateEmail(String email) {
    if (email == null || email.isEmpty) {
      return false;
    }

    // A reasonably comprehensive regex pattern for basic email validation.
    final RegExp emailRegex = RegExp(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$');
    return emailRegex.hasMatch(email);
  }

  /// Validates a phone number using a simple regex pattern.
  bool validatePhoneNumber(String phoneNumber) {
    if (phoneNumber == null || phoneNumber.isEmpty) {
      return false;
    }

    // Basic regex for phone numbers: allows optional leading '+', digits, spaces, dashes and parentheses.
    final RegExp phoneRegex = RegExp(r'^\+?[0-9\s\-()]{7,15}$');
    return phoneRegex.hasMatch(phoneNumber);
  }
}