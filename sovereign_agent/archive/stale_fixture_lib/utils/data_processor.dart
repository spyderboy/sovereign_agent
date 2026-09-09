class DataProcessor {
  /// Logs that raw input data has been successfully processed.
  void logDataProcessed(Map<String, dynamic> processedData) {
    // In a real application, this would send logs to an analytics backend.
    print('DataProcessor: Successfully logged processing of data. Key example: ${processedData['status'] ?? 'N/A'}');
  }
}