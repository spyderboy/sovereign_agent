import '../api/api_client.dart';
import '../integration/integration_interface.dart';
import 'analytics_service.dart';

class IntegrationService implements IIntegration {
  final ApiClient _apiClient = ApiClient();
  final AnalyticsService _analyticsService = AnalyticsService();

  Future<bool> connect() async {
    try {
      var result = await _apiClient.fetchData();
      if (result.isNotEmpty) {
        _analyticsService.logUserAction('connection_success');
        return true;
      } else {
        _analyticsService.logUserAction('connection_failed_no_data');
        return false;
      }
    } catch (e) {
      print('Connection error: $e');
      _analyticsService.logUserAction('connection_error');
      return false;
    }
  }
}