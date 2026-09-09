// Fix API mismatches in test/game/perf_test.dart
// (3 errors): fix broken import 'flutter_driver.dart'; resolve undefined name 'FlutterDriver'; resolve undefined name 'find' in test/game/perf_test.dart — done when: flutter analyze reports 0 errors for test/game/perf_test.dart
import 'package:flutter_driver/flutter_driver.dart';
import 'package:test/test.dart';

void main() {
  group('Performance Tests', () {
    FlutterDriver driver;

    setUpAll(() async {
      driver = await FlutterDriver.connect();
    });

    tearDownAll(() async {
      if (driver != null) {
        await driver.close();
      }
    });

    test('Test Something', () {
      expect(find.text('Hello'), findsOneWidget);
    });
  });
}