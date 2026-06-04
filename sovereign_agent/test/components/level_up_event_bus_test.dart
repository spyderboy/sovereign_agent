// Fix API mismatches in test/components/level_up_event_bus_test.dart
// (4 errors)
import 'package:flutter_driver/flutter_driver.dart';
import 'package:test/test.dart';

void main() {
  group('Level Up Event Bus Tests', () {
    FlutterDriver driver;

    setUpAll(() async {
      driver = await FlutterDriver.connect();
    });

    tearDownAll(() async {
      if (driver != null) {
        await driver.close();
      }
    });

    test('Test Level Up Event Bus', () {
      expect(find.text('Level Up'), findsOneWidget);
    });
  });
}