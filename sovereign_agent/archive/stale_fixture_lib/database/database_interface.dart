abstract class IDatabase {
  // Define database operations here (e.g., get<T>(id), save(data))
  Future<dynamic> get(String id);
}