abstract class IFileStorage {
  /// Saves the given data (bytes) to the specified file path.
  /// Returns true if successful, false otherwise.
  Future<bool> saveBytes(String path, List<int> bytes);

  /// Loads bytes from the specified file path.
  /// Returns null if the file does not exist or reading fails, otherwise returns the bytes.
  Future<List<int>?> loadBytes(String path);
}
