// Fix API mismatches in lib/components/particle_system_test.dart
// (10 errors)
import 'package:flutter/material.dart';

void main() {
  test('Particle System Test', () {
    // Create an instance of ParticleSystem with correct constructor arguments
    var particleSystem = ParticleSystem(
      maxParticles: 100,
      particleType: ParticleType.Standard,
    );

    // Access the particles property
    expect(particleSystem.particles, isNotNull);
  });
}
