class PaymentValidator {
  /// Validates a credit card number format and general length.
  /// This performs a basic structural check (digits only, plausible length).
  /// Note: Real-world validation requires Luhn algorithm checks and BIN ranges.
  bool validateCreditCard(String cardNumber) {
    if (cardNumber == null || cardNumber.isEmpty) {
      return false;
    }

    // Remove any spaces or hyphens for uniform checking
    final cleanedNumber = cardNumber.replaceAll(RegExp(r'[^0-9]'), '');

    // Basic check: must contain only digits and be within a reasonable length range (e.g., 13 to 19 digits)
    final RegExp ccRegex = RegExp(r'^[0-9]{13,19}$');

    return ccRegex.hasMatch(cleanedNumber);
  }
}