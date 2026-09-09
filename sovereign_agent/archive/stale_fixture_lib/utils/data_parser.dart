import 'dart:convert';

class DataParser {
  /// Parses a JSON string into a Map<String, dynamic>.
  /// Returns an empty map if parsing fails or the result is not a Map.
  Map<String, dynamic> parseJsonData(String json) {
    try {
      final decoded = jsonDecode(json);
      return decoded is Map<String, dynamic> ? decoded : {};
    } catch (e) {
      return {};
    }
  }

  /// Parses a raw HTTP response body (JSON string). 
  /// This function is used for API responses.
  Map<String, dynamic> parseJsonResponse(String jsonResponse) {
    try {
      final decoded = jsonDecode(jsonResponse);
      return decoded is Map<String, dynamic> ? decoded : {};
    } catch (e) {
      return {};
    }
  }
}