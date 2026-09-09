abstract class ICache {
  /// Retrieves a value associated with the given key.
  /// Returns null if the key does not exist or retrieval fails.
  T? get<T>(String key);

  /// Sets a value for a given key.
  Future<void> set<T>(String key, T value);

  /// Removes a key-value pair from the cache.
  Future<void> remove(String key);

  /// Clears all data from the cache.
  Future<void> clear();
}