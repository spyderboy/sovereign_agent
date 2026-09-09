import 'package:flutter/foundation.dart';

class NetworkMonitor {
  /// Logs network-related errors and their stack traces to the console in debug mode.
  void logNetworkError(Object error, StackTrace stackTrace) {
    if (kDebugMode) {
      print('Network Error occurred: $error');
      print('Stack trace:\n$stackTrace');
    }
  }
}