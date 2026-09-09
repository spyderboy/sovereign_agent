class DataParser {
  /// Parses raw JSON data (represented as dynamic) into a structured map.
  /// This function ensures type safety and extracts necessary fields.
  static Map<String, dynamic> parseJsonData(dynamic jsonData) {
    final result = <String, dynamic>{};

    if (jsonData == null || !(jsonData is Map<String, dynamic>)) {
      result['status'] = 'error';
      result['payload_size'] = 0.0;
      return result;
    }

    // Implement specific parsing logic here.
    try {
      final status = jsonData['status'];
      final count = jsonData['count']?.toDouble() ?? 0.0;
      
      result['status'] = (status as String?) == 'success' ? 'ok' : 'failure';
      result['payload_size'] = count;
      result['raw_data'] = jsonData;
    } catch (e) {
      print('Error parsing JSON data: $e');
      result['status'] = 'error';
      result['payload_size'] = 0.0;
    }
    
    return result;
  }
}