import 'package:flutter/foundation.dart';
import '../utils/data_parser.dart';
import '../utils/network_monitor.dart';

class ApiClient {
  final DataParser _dataParser = DataParser();
  final NetworkMonitor _networkMonitor = NetworkMonitor();

  Future<Map<String, dynamic>> fetchData() async {
    try {
      // Simulation of network data retrieval
      const String mockResponse = '{"status": "success", "data": "mock_api_result"}';

      print('Fetching data from API...');

      return await handleResponse(mockResponse);
    } catch (e) {
      print('Error during fetchData: $e');
      return {};
    }
  }

  /// Handles a raw API response string, parsing it using DataParser.
  Future<Map<String, dynamic>> handleResponse(String rawResponse) async {
    print('Handling API response...');
    return _dataParser.parseJsonData(rawResponse);
  }

  /// Executes an asynchronous API call and logs network errors if one occurs.
  /// Takes a function returning a Future which performs the core API request logic.
  Future<Map<String, dynamic>> request(Future<dynamic> Function() apiCall) async {
    try {
      final result = await apiCall();
      // Assuming successful execution results in something parsable into Map<String, dynamic>
      return result as Map<String, dynamic>;
    } catch (e, s) {
      _networkMonitor.logNetworkError(e, s);
      // Return empty map upon failure to maintain consistency with current error handling.
      return {};
    }
  }
}