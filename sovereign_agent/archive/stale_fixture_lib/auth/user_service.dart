import '../services/analytics_service.dart';

class UserService {
  final AnalyticsService analyticsService;

  UserService(this.analyticsService);

  Future<void> login() async {
    // Login logic implementation
    analyticsService.logUserAction('Login');
  }
}