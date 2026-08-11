"""
hints.py — Bad-pattern filters and flutter analyze error hints for the sovereign worker.

BAD_PATTERNS  : checked against every proposed file before it is written to disk.
                If matched, the file is rejected and the hint is fed back to the model.
ERROR_HINTS   : checked against flutter analyze output after validation fails.
                Matching hints are prepended to the error message on the next attempt.

Add new entries here as new error classes are discovered in runs.
Both lists are imported by work.py and extended at runtime by project .roorules.
"""

BAD_PATTERNS: list[tuple[str, str]] = [
    # Offset / Vector2 confusion in Flutter widget parameters
    (r'offset:\s*Vector2\b',
     "OFFSET ERROR: Transform.translate/Positioned offset: needs Flutter Offset(x,y), NOT Vector2(x,y). "
     "Flutter UI widgets use Offset; game objects use Vector2. Never pass Vector2 to a Flutter widget parameter."),

    # lengthSquared doesn't exist on Flame's Vector2
    (r'\.lengthSquared\b',
     "API ERROR: Vector2 has no .lengthSquared. Use .length, or compute (v.x*v.x + v.y*v.y) manually."),

    # Wrong Star constructor (factionId is a getter, not a named constructor)
    (r'Star\.factionId\s*\(',
     "CONSTRUCTOR ERROR: Star.factionId is a getter, NOT a constructor. "
     "Use Star.forFaction(factionId, position: Vector2(x,y)) instead."),

    # Flame Effect API (broken in this codebase — use plain Dart timers)
    (r"import.*flame/effects\.dart",
     "IMPORT ERROR: Do NOT import flame/effects.dart. Use plain Dart timer classes for animation "
     "(see _FusionAnimation in astro_game.dart for the pattern)."),
    (r'\bMoveEffect\b|\bRotateEffect\b|\bScaleEffect\b|\bOpacityEffect\b',
     "FLAME API ERROR: Don't use Flame Effect classes. Use plain Dart timer pattern instead."),

    # Canvas draw calls with Vector2 instead of Offset.
    # Allow: Offset(...), anyOffset, start.toOffset() — block bare Vector2 identifiers.
    # Old regex [^O][^f] was too broad: it also fired on correct code like
    # canvas.drawLine(start.toOffset(), ...) and canvas.drawLine(startOffset, ...).
    (r'canvas\.drawLine\s*\(\s*(?!\w*[Oo]ffset)(?!\w+\.toOffset\b)[A-Za-z_]',
     "CANVAS ERROR: canvas.drawLine() takes Offset args, not Vector2. "
     "Store fields as Vector2; convert only at the render call site: "
     "canvas.drawLine(start.toOffset(), end.toOffset(), paint)  "
     "or canvas.drawLine(Offset(start.x, start.y), Offset(end.x, end.y), paint)."),
    (r'canvas\.drawCircle\s*\(\s*\w+\.position\b',
     "CANVAS ERROR: canvas.drawCircle() takes Offset, not Vector2. "
     "Wrap: Offset(pos.x, pos.y)."),

    # Unqualified generateId
    (r'(?<!\.)generateId\(\)',
     "STATIC METHOD ERROR: generateId() must be qualified as BaseUnit.generateId(). "
     "Dart does not inherit static members."),

    # .normalized() — unreliable on older vector_math
    (r'\.normalized\(\)',
     "API ERROR: Avoid .normalized() — use (diff / distance) pattern for safe normalization."),

    # endPosition in _AnimatedMote or similar internal classes omitted
    (r'required this\.endPosition',
     "CONSTRUCTOR MISMATCH: If endPosition is required, every call site must pass it. "
     "Consider making it optional or removing it."),

    # flame/game.dart without hide Vector → ambiguous_import (vector_math also exports Vector)
    (r"import 'package:flame/game\.dart'(?! hide Vector)",
     "IMPORT ERROR: flame/game.dart re-exports vector_math's Vector type. "
     "ALWAYS write: import 'package:flame/game.dart' hide Vector; "
     "Omitting 'hide Vector' causes ambiguous_import at every use of our game unit Vector."),

    # NOTE: super.update(dt) is VALID and REQUIRED in FlameGame subclasses.
    # Do NOT add a bad pattern for super.update(dt) — FlameGame.update is not abstract.

    # .notifier() called as a function — StateNotifierProvider.notifier is a getter, not a method
    (r'\.notifier\s*\(\s*\)',
     "RIVERPOD ERROR: .notifier is a getter on StateNotifierProvider, NOT a method. "
     "Never call gameServiceProvider.notifier() with parentheses. "
     "Correct usage: container.read(gameServiceProvider.notifier) — no parentheses."),

    # fuseWith is game logic — must never be added to pure data models (Mote, Vector, Star)
    (r'def fuseWith|void fuseWith|Vector\? fuseWith|fuseWith\(',
     "MODEL PURITY ERROR: fuseWith() is game logic and must NOT be added to Mote, Vector, or Star. "
     "These are pure data classes (id + toMap/fromMap only). "
     "Fusion logic belongs in lib/game/ or lib/systems/, not in models/."),

    # AudioPlayerEffect is an invented class
    (r'\bAudioPlayerEffect\b',
     "INVENTED CLASS ERROR: AudioPlayerEffect does not exist in flame_audio. "
     "Use FlameAudio.play(filename) for one-shot effects. No wrapper class needed."),

    # Bgm is not a public type — access via FlameAudio.bgm, never instantiate or type-annotate
    (r'\bBgm\b',
     "FLAME AUDIO ERROR: 'Bgm' is not a public type you can import or annotate. "
     "Access the background music player via FlameAudio.bgm (no import needed). "
     "Never write 'Bgm player' or 'late Bgm _bgm' — just call FlameAudio.bgm.play(...) directly."),

    # FlameAudio.bgm.isPlaying is a getter, not a method
    (r'FlameAudio\.bgm\.isPlaying\s*\(',
     "FLAME AUDIO ERROR: FlameAudio.bgm.isPlaying is a bool getter, not a method. "
     "Use 'if (FlameAudio.bgm.isPlaying)' not 'if (FlameAudio.bgm.isPlaying())'"),

    # withOpacity is deprecated in Flutter 3.x — use withValues(alpha:)
    (r'\.withOpacity\(',
     "DEPRECATION ERROR: .withOpacity() is deprecated. "
     "Use .withValues(alpha: x) instead."),

    # Colors.magenta does not exist in Flutter — use Colors.purple
    (r'Colors\.magenta\b',
     "COLOR ERROR: Colors.magenta does not exist in Flutter. "
     "Use Colors.purple (enemy faction color throughout this codebase)."),

    # const Vector2(...) — Vector2 is not const-constructable (backed by Float64List)
    (r'const\s+Vector2\s*\(',
     "CONST ERROR: Vector2 is NOT const-constructable. "
     "Remove 'const': use Vector2(x, y) not const Vector2(x, y)."),

    # Non-const instance field in a class that has a const constructor
    # e.g. class Foo { const Foo(); final List<...> items = [Bar()]; }  ← Bar() not const
    (r'const\s+\w+\(\{[^}]*super\.key[^}]*\}\)[\s\S]{0,400}final\s+\w+[^=]+=\s*\[',
     "CONST CONSTRUCTOR ERROR: A class with 'const MyWidget({super.key})' cannot have "
     "non-const field initializers (e.g. 'final list = [SomeClass()]'). "
     "Move the field initializer into the build() method as a local variable instead."),

    # Hallucinated / typo'd package names — models invent these under pressure
    (r"flutter_riverpod/flutter_river(?!pod\.dart)",
     "IMPORT TYPO: The correct import is 'package:flutter_riverpod/flutter_riverpod.dart'. "
     "Do NOT use flutter_riverpad, flutter_riverpost, or any other variant."),

    (r"package:flame/(?:math_engine|geometry_engine|physics_engine|vector_engine|render_engine)\b",
     "HALLUCINATED PACKAGE: flame/math_engine.dart (and similar) do not exist. "
     "Use package:flame/components.dart for Vector2/Component, dart:math for math utilities."),

    # Swift/Kotlin-style optional cast — not valid Dart
    (r'\bas\?\s+\w',
     "SYNTAX ERROR: Dart does not have 'as?' optional casting (that's Swift/Kotlin). "
     "Use 'if (x is MyType)' or 'x is MyType ? x as MyType : null' instead."),

    # Offset used as start/end for AttackLineComponent — which expects Vector2
    (r'start:\s*Offset\s*\(|end:\s*Offset\s*\(',
     "TYPE ERROR: AttackLineComponent.start and .end are Vector2, not Offset. "
     "Use position.clone() or Vector2(x, y) — never Offset(x, y) for Flame component fields."),
]

# ─── Error → targeted hint mapping ───────────────────────────────────────────
# When flutter analyze output matches a pattern, the hint is prepended to the
# errors fed back to the 35B on the next retry, giving it precise guidance.
ERROR_HINTS: list[tuple[str, str]] = [
    (r"can.t be assigned to the parameter type 'int'|argument_type_not_assignable.*\bint\b",
     "⚠️  STRING passed where INT expected. All model IDs are int, never String:\n"
     "  Star.id → int,  Vector.id → int,  Star.ownerId → String (player label only).\n"
     "  CaptureRules.tryPerformCapture(notifier, vectorId: int, starId: int) — no player string.\n"
     "  CombatAttackEvent(sourceVectorId: int, targetStarId: int) — both int.\n"
     "  Never pass ownerId/playerOwnerId where an id parameter is expected."),

    (r'argument_type_not_assignable.*Offset|can.t be assigned.*Offset',
     "⚠️  OFFSET vs VECTOR2: Flutter widget parameters (offset:, position:, etc.) "
     "require Offset(x, y) — NEVER pass Vector2. "
     "Offset has .dx/.dy; Vector2 has .x/.y. They are incompatible types."),

    (r'undefined_getter.*lengthSquared|undefined_method.*lengthSquared',
     "⚠️  Vector2 has NO .lengthSquared — use .length or compute v.x*v.x + v.y*v.y manually."),

    (r'library_private_types_in_public_api',
     "⚠️  Private types (names starting with _) cannot appear in public class fields/methods. "
     "Either rename the private type to be public, or make the containing class private (_ClassName)."),

    (r'undefined_function.*Vector2|undefined_method.*Vector2',
     "⚠️  Vector2 is not imported. Add: import 'package:flame/components.dart' hide Vector;"),

    (r'missing_required_argument',
     "⚠️  A required constructor parameter is missing. Read the class definition carefully "
     "and provide ALL required: named parameters."),

    (r'extra_positional_arguments|2 positional arguments expected by .AsyncError|'
     r'2 positional arguments expected by .error',
     "⚠️  AsyncError requires TWO positional arguments: AsyncError(error, stackTrace). "
     "Never call AsyncError(e) with one argument — Dart requires the stack trace too. "
     "Pattern: catch (e, st) { return AsyncValue.error(e, st); } "
     "or AsyncError(e, StackTrace.current) if outside a catch block."),

    (r'extends_non_class|non_type_as_type_argument',
     "⚠️  extends_non_class: you are extending something that is not a class. "
     "Common Riverpod mistakes: do NOT write 'extends AsyncNotifier' without the generic "
     "type parameter, do NOT extend a provider (e.g. 'extends FusionProvider'), "
     "and do NOT extend abstract classes that require type args without providing them. "
     "Correct patterns: 'class X extends StateNotifier<MyState>', "
     "'class X extends AsyncNotifier<MyType>'."),

    (r'undefined_method.*distanceTo|Offset.*distanceTo',
     "⚠️  Offset has no distanceTo(). Use (o - Offset(v.x, v.y)).distance instead."),

    (r'undefined_method.*normalized\b',
     "⚠️  .normalized() is unreliable — use (diff / distance) pattern: "
     "final n = diff / diff.length;"),

    (r'argument_type_not_assignable.*List<Nova>|argument_type_not_assignable.*List<Vector>',
     "⚠️  Type mismatch: use typed lists <Nova>[], <Vector>[] — not <dynamic>[]."),

    (r'ambiguous_import.*Vector|Vector.*ambiguous_import',
     "⚠️  ambiguous_import for 'Vector': flame/game.dart re-exports vector_math's Vector. "
     "Fix: import 'package:flame/game.dart' hide Vector;"),

    (r'abstract_super_member_reference.*update|update.*abstract_super_member_reference',
     "⚠️  Game.update(double dt) is abstract — remove super.update(dt) from your override. "
     "Just call your own logic directly."),

    (r'deprecated_member_use.*withOpacity|withOpacity.*deprecated',
     "⚠️  .withOpacity() is deprecated — use .withValues(alpha: x) instead."),

    (r'const_with_non_const',
     "⚠️  const_with_non_const: a widget class has 'const' constructor but a non-const "
     "field initializer (e.g. 'final list = [SomeClass()]'). "
     "Fix: move the field into build() as a local variable, NOT an instance field."),

    (r'creation_with_non_type.*Vector2|Vector2.*creation_with_non_type',
     "⚠️  Vector2 is not in scope. Add: import 'package:flame/components.dart'; "
     "(use 'hide Vector' if also importing models/vector.dart). "
     "Also: NEVER use 'const Vector2(...)' — Vector2 is not const-constructable."),

    (r'annotate_overrides',
     "⚠️  annotate_overrides: a method/getter overrides a parent but is missing '@override'. "
     "Add @override on the line immediately before the method/getter declaration."),

    (r"isn't defined.*Level1|Level1.*isn't defined",
     "⚠️  Level API: there is NO 'Level1' class. The correct class is 'Level001' in "
     "lib/levels/level_001.dart. Its definition is a static getter: Level001.definition "
     "which returns a LevelDef — NOT stars/motes/vectors directly. "
     "For tests, construct Star objects manually using Faction.player / Faction.enemy, "
     "call game.initialize([star], [], [], []), set game.state.gameState = 'playing', "
     "then call game.update(1.0) in a loop."),

    (r'uri_does_not_exist',
     "⚠️  uri_does_not_exist: a package import cannot be resolved. "
     "This means the package is missing from pubspec.yaml OR flutter pub get has not been run. "
     "Do NOT remove the import — the package is already in pubspec.yaml. "
     "This error will clear on its own once 'flutter pub get' is run. "
     "Rewrite the file with the same imports unchanged."),

    (r"positional argument.*GameStateNotifier|GameStateNotifier.*positional argument",
     "⚠️  GameStateNotifier requires ONE positional argument: a PersistenceService. "
     "Correct pattern:\n"
     "  import 'package:astro_flux/systems/local_persistence_service.dart';\n"
     "  final notifier = GameStateNotifier(LocalPersistenceService());\n"
     "NEVER call GameStateNotifier() with no arguments — it will not compile."),

    (r"isn't defined for the type 'StarCaptureIndicator'|"
     r"_progress.*StarCaptureIndicator|_captured.*StarCaptureIndicator",
     "⚠️  StarCaptureIndicator private fields (_progress, _captured) cannot be accessed "
     "directly from tests. Use the public @visibleForTesting getters instead:\n"
     "  indicator.progress   (double, 0.0–1.0)\n"
     "  indicator.captured   (bool)\n"
     "Do NOT access _progress or _captured directly."),

    (r"components/stars/star_capture_indicator|"
     r"Target of URI doesn't exist.*star_capture_indicator",
     "⚠️  StarCaptureIndicator import path: the file lives at "
     "lib/components/star_capture_indicator.dart. "
     "Import it as: import 'package:astro_flux/components/star_capture_indicator.dart';\n"
     "The path components/stars/star_capture_indicator.dart is a re-export alias — "
     "prefer the canonical path in new code."),

    (r"Target of URI doesn't exist.*level_up_event_bus|"
     r"level_up_event_bus.*doesn't exist",
     "⚠️  level_up_event_bus.dart does not exist in this project. "
     "Remove any import of 'package:astro_flux/game/level_up_event_bus.dart' — "
     "it is an unused import generated in error. Do not create the file."),

    (r"part of.*astro_flux\.|part of.*library",
     "⚠️  Do NOT use 'part of' directives. This project does not use Dart part files. "
     "Every .dart file must be a standalone library. "
     "Remove any 'part of <library>;' line at the top of the file."),

    (r"game_core\.dart|import.*game_core|GameCore.*isn't a function|isn't a function.*GameCore",
     "⚠️  GameCore is NOT a class you can instantiate. Do not call GameCore(...). "
     "The correct pattern for embedding the game in Flutter is Flame's built-in GameWidget:\n"
     "  import 'package:flame/game.dart';\n"
     "  import 'package:astro_flux/game/astro_game.dart';\n"
     "  GameWidget<AstroGame>(\n"
     "    game: AstroGame(),\n"
     "    overlayBuilderMap: {'gameOver': (ctx, game) => GameOverOverlay()},\n"
     "  )\n"
     "Never use GameCore(...) — use GameWidget<AstroGame>(...) instead."),

    (r"game_rules_engine\.dart|import.*game_rules_engine",
     "⚠️  game_rules_engine.dart does not contain game logic — it is a re-export stub. "
     "Game rules live in: lib/game/capture_rules.dart and lib/game/fusion_rules.dart. "
     "Import those directly instead."),

    (r"StateNotifier.*not found|StateNotifierProvider.*not found|"
     r"Type 'StateNotifier' not found|Method not found: 'StateNotifierProvider'",
     "⚠️  RIVERPOD 3.x: StateNotifier and StateNotifierProvider are REMOVED.\n"
     "  Migrate to Notifier<T> + NotifierProvider:\n"
     "  OLD (broken):\n"
     "    class MyNotifier extends StateNotifier<MyState> {\n"
     "      MyNotifier() : super(MyState.initial);\n"
     "      void doSomething() { state = newState; }\n"
     "    }\n"
     "    final myProvider = StateNotifierProvider<MyNotifier, MyState>((ref) => MyNotifier());\n"
     "  NEW (correct):\n"
     "    class MyNotifier extends Notifier<MyState> {\n"
     "      @override\n"
     "      MyState build() => MyState.initial;  // replaces super(initialState)\n"
     "      void doSomething() { state = newState; }  // state getter/setter unchanged\n"
     "    }\n"
     "    final myProvider = NotifierProvider<MyNotifier, MyState>(() => MyNotifier());\n"
     "  ref.read(myProvider.notifier) still works. ref.watch(myProvider) still works.\n"
     "  WidgetRef and Ref are SEPARATE types in Riverpod 3.x — do not pass WidgetRef as Ref."),

    # Vector2 .dx / .dy misuse
    (r"getter 'dx' isn't defined.*Vector2|getter 'dy' isn't defined.*Vector2|"
     r"Vector2.*getter 'dx'.*isn't defined|Vector2.*getter 'dy'.*isn't defined",
     "⚠️  VECTOR2 HAS NO .dx/.dy — those are Offset (dart:ui) properties.\n"
     "  Vector2 uses .x and .y.\n"
     "  WRONG: v.dx, v.dy\n"
     "  RIGHT: v.x,  v.y\n"
     "  To convert Vector2 → Offset: Offset(v.x, v.y)  or  v.toOffset()"),

    (r"argument type 'WidgetRef' can't be assigned to.*'Ref'|"
     r"WidgetRef.*can't be assigned.*Ref",
     "⚠️  RIVERPOD 3.x: WidgetRef and Ref are SEPARATE types — cannot pass WidgetRef as Ref.\n"
     "  To give a long-lived object (e.g. AstroGame) a proper Ref, create it inside a Provider:\n"
     "    final myGameProvider = Provider<AstroGame>((ref) => AstroGame(ref));\n"
     "  Then read it in initState:\n"
     "    _game = ref.read(myGameProvider);  // ref here is WidgetRef, but the game gets Ref\n"
     "  Never pass the WidgetRef from ConsumerState.ref directly to a Ref parameter."),

    (r"Classes can only extend other classes|extends.*Provider|extends.*Notifier(?!<)",
     "⚠️  A class is trying to extend a provider or non-class type. "
     "Riverpod providers are not classes you extend. Correct patterns:\n"
     "  class MyNotifier extends Notifier<MyState> { ... }  // Riverpod 3.x\n"
     "  final myProvider = NotifierProvider<MyNotifier, MyState>(() => MyNotifier());\n"
     "Never write 'extends fusionProvider' or 'extends NotifierProvider'."),

    (r"valueOrNull.*ConnectivityResult|ConnectivityResult.*valueOrNull",
     "⚠️  AsyncValue<List<ConnectivityResult>> does not have valueOrNull in this Riverpod version. "
     "Use .when() or .value instead:\n"
     "  final result = ref.watch(connectivityProvider);\n"
     "  final isOnline = result.value?.contains(ConnectivityResult.wifi) ?? false;\n"
     "Or use connectivityProvider as a plain Provider<ConnectivityResult> if async is not needed."),

    (r"game_events\.dart|VectorFusedEvent|LevelUpEventBus",
     "⚠️  game_events.dart, VectorFusedEvent, and LevelUpEventBus do not exist in this project. "
     "Do not import or reference them. For fusion events use canFuseProvider from "
     "lib/game/fusion_provider.dart which reads mote count directly from gameServiceProvider."),

    (r"toVector2.*isn't defined.*Vector2|Vector2.*toVector2.*isn't defined",
     "⚠️  .toVector2() DOES NOT EXIST ON Vector2 — it's already a Vector2.\n"
     "  In Flame's ScaleUpdateInfo:\n"
     "    info.delta.global  → Vector2 (already)  — use it directly\n"
     "    info.scale.global  → Vector2 (already)  — use .x or .y directly\n"
     "  Only call .toVector2() on Offset or other non-Vector2 types.\n"
     "  CORRECT: camera.viewfinder.position -= info.delta.global / zoom;\n"
     "  WRONG:   camera.viewfinder.position -= info.delta.global.toVector2() / zoom;"),

    (r"ScaleDetector.*can't be mixed|can't be mixed.*ScaleDetector"
     r"|Classes can only mix in mixins.*gesture|Classes can only extend.*GestureHandler",
     "⚠️  FLAME GESTURE MIXIN ERROR: ScaleDetector, TapDetector, DragCallbacks etc. can only be\n"
     "  mixed onto FlameGame (or a Component that satisfies their 'on' constraint).\n"
     "  NEVER create 'class GestureHandler extends PositionComponent with ScaleDetector' — invalid.\n"
     "  NOTE: AstroGame does NOT use ScaleDetector — pinch zoom is via Listener in main.dart.\n"
     "  Gesture components should extend PositionComponent with TapCallbacks (see GestureHandler):\n"
     "    class GestureHandler extends PositionComponent with TapCallbacks {\n"
     "      @override void onScaleStart(ScaleStartInfo info) { ... }\n"
     "      @override void onScaleUpdate(ScaleUpdateInfo info) {\n"
     "        if (info.pointerCount >= 2) {\n"
     "          camera.viewfinder.zoom = newZoom.clamp(0.4, 2.0);\n"
     "          camera.viewfinder.position -= delta / camera.viewfinder.zoom;\n"
     "        }\n"
     "      }\n"
     "    }\n"
     "  AstroGame already has ScaleDetector wired — do NOT rewrite it.\n"
     "  For tap handling on individual components use 'with TapCallbacks' on the COMPONENT."),

    (r"CombatResultLabel.*isn't defined|capture_result_label|combat_result_label\.dart",
     "⚠️  Do NOT create a new capture/combat label component or file. The infrastructure already exists:\n"
     "  • lib/game/capture_event_notifier.dart — CaptureMessageNotifier (StateNotifier<String>)\n"
     "  • captureMessageProvider — Provider<String> you can watch with Consumer or ref.watch()\n"
     "  To display the message, add a Consumer widget that watches captureMessageProvider and\n"
     "  shows an Overlay or AnimatedSwitcher. Do NOT create capture_result_label_component.dart\n"
     "  or combat_result_label.dart — those files do not exist and should not be created.\n"
     "  The locked CombatResultLabelComponent at lib/components/combat_result_label_component.dart\n"
     "  is a Flame PositionComponent for in-world labels — use CaptureMessageNotifier for HUD toasts."),

    (r"firebase_remote_config|firebase_core|cloud_firestore|firebase_auth",
     "⚠️  Firebase packages are NOT in this project. Do not import any firebase_* package.\n"
     "  This project uses LocalPersistenceService (in-memory) for persistence — no Firebase.\n"
     "  Remove all firebase_remote_config, firebase_core, cloud_firestore imports immediately."),

    (r"flutter_vector_math|package:vector_math/vector_math_64|vector_math\.dart",
     "⚠️  Do NOT import flutter_vector_math or vector_math directly.\n"
     "  Vector2 comes from Flame: 'import package:flame/components.dart' (or package:flame/game.dart).\n"
     "  Both already re-export vector_math's Vector2. Never add a separate vector_math import."),

    (r"Directives must appear before any declarations|directive.*before.*declaration",
     "⚠️  IMPORT AFTER CLASS: All 'import' statements must appear at the TOP of the file,\n"
     "  before any class, enum, or function declarations.\n"
     "  Move every import to lines 1-N before the first 'class' or 'enum' keyword."),

    (r"Target of URI doesn't exist.*'../game_state_provider|'../game_state_provider",
     "⚠️  WRONG IMPORT PATH for game_state_provider.dart.\n"
     "  The file is at lib/models/game_state_provider.dart.\n"
     "  From lib/game/*.dart:    import '../models/game_state_provider.dart';\n"
     "  From lib/game/ai/*.dart: import '../../models/game_state_provider.dart';\n"
     "  ALWAYS SAFE: import 'package:astro_flux/models/game_state_provider.dart';\n"
     "  NEVER write '../game_state_provider.dart' — there is no game_state_provider in lib/game/."),

    (r"'Mote' isn't a function|Mote.*isn't a function",
     "⚠️  'Mote' IS A CLASS, not a function. Never call it positionally.\n"
     "  Correct constructor: Mote(id: someInt)\n"
     "  Mote has NO position field — it only has: id (int), lifecycleState (MoteLifecycle).\n"
     "  To check if active: mote.isActive  (getter, not a method call)\n"
     "  Do NOT write: Mote(id, position), Mote(id), or state.motes.map(Mote).\n"
     "  Lifecycle helpers return new instances: mote.setActive(), mote.setFused(), mote.setCreated()"),

    (r"'text' can't be used as a setter.*final|final.*'text'.*setter",
     "⚠️  CombatResultLabelComponent.text is final — you cannot mutate it after construction.\n"
     "  To show a new label, remove the old component and add a new one:\n"
     "    parent.remove(oldLabel);\n"
     "    parent.add(CombatResultLabelComponent(text: 'Captured!', color: Colors.green));\n"
     "  Do NOT write: label.text = 'something'; — that will always fail with a setter error."),

    (r"Classes can only extend other classes.*capture|capture.*Classes can only extend",
     "⚠️  CaptureEventNotifier / CaptureMessageNotifier must extend StateNotifier<String>, not a provider.\n"
     "  CORRECT:\n"
     "    class CaptureMessageNotifier extends StateNotifier<String> {\n"
     "      CaptureMessageNotifier() : super('');\n"
     "    }\n"
     "  WRONG: extends captureMessageProvider, extends StateNotifierProvider, extends Provider.\n"
     "  Providers are instances created by the framework — you never extend them."),

    (r"isn't a valid override of.*PositionComponent|CombatResultLabelComponent.*position.*isn't",
     "⚠️  Do NOT declare 'final Vector2 position' as an instance field in a PositionComponent subclass. "
     "PositionComponent already has a 'position' property — re-declaring it causes an override conflict. "
     "Instead, accept the initial position as a constructor parameter named 'initialPosition' and pass "
     "it only to super():\n"
     "  class MyComponent extends PositionComponent {\n"
     "    MyComponent({Vector2? initialPosition}) : super(position: initialPosition ?? Vector2.zero());\n"
     "    // Use 'position' (inherited) directly — never redeclare it as a field.\n"
     "  }"),

    (r"Target of URI doesn't exist.*audio_service|audio_service.*Target of URI"
     r"|'../../audio_service\.dart'|'../audio_service\.dart'",
     "⚠️  WRONG IMPORT PATH for audio_service.dart. The canonical file is at lib/services/audio_service.dart.\n"
     "  From lib/game/ai/*.dart use:      import '../../services/audio_service.dart';\n"
     "  From lib/game/*.dart use:         import '../services/audio_service.dart';\n"
     "  From lib/game_ui/*.dart use:      import '../services/audio_service.dart';\n"
     "  From lib/components/*.dart use:   import '../services/audio_service.dart';\n"
     "  ALWAYS SAFE: import 'package:astro_flux/services/audio_service.dart';\n"
     "  NEVER write: import '../audio_service.dart' or import '../../audio_service.dart' — "
     "audio_service.dart lives in services/, not in game/ or game/ai/."),

    (r"Target of URI doesn't exist.*capture_rules|capture_rules.*Target of URI",
     "⚠️  WRONG IMPORT PATH for capture_rules.dart. The canonical file is at lib/game/capture_rules.dart.\n"
     "  From lib/game/ai/*.dart use:      import '../capture_rules.dart';\n"
     "  From lib/game_ui/*.dart use:      import '../game/capture_rules.dart';\n"
     "  From lib/components/*.dart use:   import '../game/capture_rules.dart';\n"
     "  Or always safe: import 'package:astro_flux/game/capture_rules.dart';"),

    (r"body might complete normally.*'null'.*return type.*'bool'|non_nullable_return_type",
     "⚠️  METHOD MISSING RETURN STATEMENT: A method declared to return 'bool' (or another non-nullable type) "
     "has no return statement — the body completes without returning a value. Add an explicit return:\n"
     "  static bool canAttack(...) {\n"
     "    if (someCondition) return false;\n"
     "    return true;  // ← must always return\n"
     "  }"),

    (r"package:particle_effects|Target of URI doesn't exist.*particle_effects\.dart'(?!.*astro_flux)",
     "⚠️  'package:particle_effects' does NOT exist — it is a hallucinated external package.\n"
     "  The particle system is internal to this project:\n"
     "    import 'package:astro_flux/game/particle_effects.dart';   // ParticleEffects, CombatEffect\n"
     "    import 'package:astro_flux/game/particle_system.dart';    // ParticleSystem\n"
     "  API: ParticleEffects(ParticleSystem system).trigger(CombatEffect, Vector2, Color)\n"
     "  CombatEffect values: attack, hit, destroy, heal\n"
     "  NEVER import from 'package:particle_effects/...' — that package is not in pubspec.yaml."),

    (r"'CombatAttackEvent' isn't a function|CombatAttackEvent.*isn't a function",
     "⚠️  CombatAttackEvent IS a class — use its named constructor, never call it positionally.\n"
     "  Correct: CombatAttackEvent(sourceVectorId: 1, targetStarId: 2)\n"
     "  Import:  import 'package:astro_flux/game/combat_attack_event.dart';\n"
     "  NEVER write: CombatAttackEvent(1, 2) — both parameters are named and required.\n"
     "  The class has exactly two fields: sourceVectorId (int) and targetStarId (int)."),

    (r"method 'addMoveOrder' isn't defined.*'AstroGame'|"
     r"method 'issueOrder' isn't defined.*'AstroGame'|"
     r"method 'dispatchOrder' isn't defined.*'AstroGame'|"
     r"method 'sendUnit' isn't defined.*'AstroGame'",
     "⚠️  HALLUCINATED METHOD on AstroGame: addMoveOrder/issueOrder/dispatchOrder/sendUnit do NOT exist.\n"
     "  AstroGame is a FlameGame render host — it has NO game-logic command methods.\n"
     "  To move a unit or issue an order, go through the Riverpod notifier:\n"
     "    _ref.read(gameServiceProvider.notifier).moveVector(vectorId, targetStarId);\n"
     "  EnemyAI must hold 'final Ref _ref' and call notifier methods — never call methods on AstroGame.\n"
     "  Check lib/game/game_state_notifier.dart for the actual available methods."),

    (r"argument type 'Offset' can't be assigned to the parameter type 'Vector2'|"
     r"type 'Offset' is not a subtype of type 'Vector2'",
     "⚠️  Offset IS NOT Vector2 — they are incompatible types.\n"
     "  Convert manually wherever a Vector2 is required:\n"
     "    WRONG: someFlameMethod(myOffset)\n"
     "    RIGHT: someFlameMethod(Vector2(myOffset.dx, myOffset.dy))\n"
     "  Offset has .dx/.dy;  Vector2 has .x/.y.\n"
     "  Rule: Flame game canvas = Vector2.  Flutter widget layer = Offset.\n"
     "  In GestureDetector callbacks, localPosition is Offset — convert before passing to Flame."),

    (r"getter 'state' isn't defined.*'AstroGame'|"
     r"'AstroGame'.*getter 'state'.*isn't defined|"
     r"_game\.state\b|game\.state\b",
     "⚠️  AstroGame has NO '.state' getter — never call _game.state or game.state.\n"
     "  AstroGame is a FlameGame subclass, not a state container.\n"
     "  To read game state from inside AI/handler classes:\n"
     "    final state = _ref.read(gameServiceProvider);   // _ref is a Riverpod Ref\n"
     "  To mutate state:\n"
     "    _ref.read(gameServiceProvider.notifier).someMethod();\n"
     "  EnemyAI and all AI classes must hold a 'final Ref _ref' — NOT a reference to AstroGame.\n"
     "  NEVER write: _game.state, game.state, _astroGame.state"),

    (r"getter 'state' isn't defined.*GameStateNotifier|"
     r"'state'.*isn't defined.*type 'GameStateNotifier'|"
     r"_gameStateNotifier\.state",
     "⚠️  GameStateNotifier does NOT expose a public 'state' getter — never access .state on it.\n"
     "  To READ game state:    final state = ref.read(gameServiceProvider);\n"
     "  To MUTATE game state:  ref.read(gameServiceProvider.notifier).someMethod();\n"
     "  CombatAttackHandler and UnitCombatResolver both take a Ref, not a GameStateNotifier:\n"
     "    class CombatAttackHandler { final Ref _ref; CombatAttackHandler(this._ref); }\n"
     "  NEVER write: notifier.state  or  _gameStateNotifier.state"),

    # ── New entries added 2026-05-30 round 2 (learned from run analysis) ──────

    (r"mixin_with_clause|A mixin can't have a with clause",
     "⚠️  MIXIN WITH CLAUSE: A mixin declaration cannot use 'with'. "
     "Mixins use 'on' to constrain what they can be applied to — not 'with'.\n"
     "  WRONG: mixin UnitShatterComponent with PositionComponent { ... }\n"
     "  RIGHT: mixin UnitShatterComponent on PositionComponent { ... }\n"
     "  Or if UnitShatterComponent should be a regular class:\n"
     "  RIGHT: class UnitShatterComponent extends PositionComponent { ... }\n"
     "  Rule: 'mixin Foo on Bar' constrains Foo to only be mixed onto Bar subclasses.\n"
     "  'mixin Foo with Bar' is a Dart syntax error — it does not exist."),

    (r"'expect' isn't defined|The function 'expect' isn't defined|"
     r"'expect'.*isn't defined.*Try importing",
     "⚠️  MISSING TEST IMPORT: 'expect' is defined in package:flutter_test.\n"
     "  Every test file must start with these imports:\n"
     "    import 'package:flutter_test/flutter_test.dart';\n"
     "    import 'package:flutter_riverpod/flutter_riverpod.dart';  // if using ProviderContainer\n"
     "  Add them as the very first lines of the file, before any other imports.\n"
     "  Also ensure the test file has a void main() { } entry point wrapping all test() calls."),

    (r"'ProviderContainer' isn't defined|The function 'ProviderContainer' isn't defined|"
     r"ProviderContainer.*isn't defined.*Try importing",
     "⚠️  MISSING RIVERPOD TEST IMPORT: ProviderContainer is in flutter_riverpod.\n"
     "  Add to the top of the test file:\n"
     "    import 'package:flutter_riverpod/flutter_riverpod.dart';\n"
     "  Standard test boilerplate for Riverpod:\n"
     "    final container = ProviderContainer();\n"
     "    addTearDown(container.dispose);\n"
     "    final result = container.read(myProvider);\n"
     "  Never call ProviderContainer without first importing flutter_riverpod."),

    (r"value of type 'double' can't be assigned to.*'int'|"
     r"type 'double' is not a subtype of type 'int'",
     "⚠️  DOUBLE TO INT: A double value is being assigned to an int field.\n"
     "  Fix options:\n"
     "    1. Convert explicitly:  myInt = myDouble.toInt();\n"
     "    2. Round:               myInt = myDouble.round();\n"
     "    3. Change the field:    int myField → double myField\n"
     "  Common cause: arithmetic like 'width / 2' returns double even when both operands "
     "are int. Use integer division instead: 'width ~/ 2' returns int.\n"
     "  NEVER use 'as int' cast on a double — it will throw at runtime."),

    # ── New entries added 2026-05-30 round 3 (learned from run analysis) ──────

    (r"Only classes and mixins can be used as superclass constraints|"
     r"mixin_superclass_constraint_non_interface",
     "⚠️  MIXIN ON CONSTRAINT: The class after 'on' in a mixin declaration must be a "
     "concrete class or another mixin — not an abstract class, interface, or typedef.\n"
     "  WRONG: mixin Foo on SomeAbstractInterface { ... }\n"
     "  RIGHT: mixin Foo on PositionComponent { ... }   // PositionComponent is a class\n"
     "  RIGHT: mixin Foo on Component { ... }           // Component is a class\n"
     "  If you want Foo to work on any PositionComponent subclass, use:\n"
     "    mixin Foo on PositionComponent { ... }\n"
     "  then apply it: class Bar extends PositionComponent with Foo { ... }"),

    (r"Undefined class 'Vector2'.*path_preview|"
     r"Undefined class 'Vector2'.*model|Undefined class 'Vector2'.*state|"
     r"Undefined class 'Vector2'.*notifier|Undefined class 'Vector2'.*provider",
     "⚠️  VECTOR2 IMPORT: Vector2 is not automatically available in model/state/notifier "
     "files — it must be explicitly imported.\n"
     "  Add one of:\n"
     "    import 'package:flame/components.dart';  // preferred in Flame projects\n"
     "    import 'package:vector_math/vector_math_64.dart';  // if not using Flame\n"
     "  Alternatively, use dart:ui Offset for pure model classes that don't need Flame:\n"
     "    import 'dart:ui'; then use Offset(x, y) instead of Vector2(x, y)"),

    (r"Target of URI doesn't exist.*_provider\.dart|"
     r"Target of URI doesn't exist.*_notifier\.dart|"
     r"Target of URI doesn't exist.*_service\.dart",
     "⚠️  INVENTED IMPORT: You imported a file that does not exist in this project.\n"
     "  Before writing any import, verify the file is in the provided file list.\n"
     "  NEVER import a provider, notifier, or service file you just invented.\n"
     "  If you need something from a provider that doesn't exist yet:\n"
     "    Option A: Create that provider in the SAME response as the component.\n"
     "    Option B: Remove the import and hardcode a sensible default for now.\n"
     "  Writing an import to a non-existent file will always fail flutter analyze."),

    (r"'FlameGame' isn't defined.*test|The function 'FlameGame' isn't defined",
     "⚠️  FLAMEGAME IN TEST: FlameGame is not auto-imported in test files.\n"
     "  Add to the top of the test file:\n"
     "    import 'package:flame/game.dart';         // FlameGame, GameWidget\n"
     "    import 'package:flame_test/flame_test.dart';  // testWithFlameGame helper\n"
     "  Preferred pattern for Flame component tests:\n"
     "    testWithFlameGame('description', (game) async {\n"
     "      final component = MyComponent();\n"
     "      await game.ensureAdd(component);\n"
     "      expect(component.isMounted, isTrue);\n"
     "    });\n"
     "  Do NOT instantiate AstroGame directly in tests — use testWithFlameGame."),

    (r"method 'Paint' isn't defined|'Paint' isn't defined for the type|"
     r"The method 'Paint' isn't defined",
     "⚠️  PAINT IS TOP-LEVEL: Paint() is a top-level constructor from dart:ui — "
     "it is NOT a method on your class.\n"
     "  WRONG: final p = this.Paint();   // Paint is not a method\n"
     "  WRONG: Paint paint = Paint();    // missing import\n"
     "  RIGHT:\n"
     "    import 'dart:ui';  // or comes via package:flutter/material.dart\n"
     "    final paint = Paint()\n"
     "      ..color = Colors.cyan\n"
     "      ..blendMode = BlendMode.plus;\n"
     "  In Flame components, dart:ui is usually already available via "
     "'import package:flame/components.dart'. No extra import needed there."),

    # ── New entries added 2026-05-30 round 4 ────────────────────────────────

    (r"mixin_declares_constructor|Mixins can't declare constructors",
     "⚠️  MIXIN CONSTRUCTOR: Mixins cannot declare constructors — this is a Dart rule.\n"
     "  WRONG:\n"
     "    mixin CameraShakeMixin {\n"
     "      CameraShakeMixin(this.camera);  // ← illegal\n"
     "    }\n"
     "  RIGHT — use fields with late initialisation or an init() method instead:\n"
     "    mixin CameraShakeMixin {\n"
     "      late CameraComponent _camera;\n"
     "      void initShake(CameraComponent camera) { _camera = camera; }\n"
     "    }\n"
     "  Or convert to a regular class if you need a constructor:\n"
     "    class CameraShakeController {\n"
     "      CameraShakeController(this._camera);\n"
     "      final CameraComponent _camera;\n"
     "    }"),

    (r"argument type 'ProviderContainer' can't be assigned to the parameter type 'Ref'|"
     r"ProviderContainer.*can't be assigned.*Ref",
     "⚠️  PROVIDERCONTAINER IS NOT REF: ProviderContainer and Ref are different types.\n"
     "  ProviderContainer is test infrastructure — never pass it to production code.\n"
     "  WRONG (in test):\n"
     "    final container = ProviderContainer();\n"
     "    final system = PulsarFlareSystem(container);  // container is not a Ref\n"
     "  RIGHT — read the value directly from the container:\n"
     "    final container = ProviderContainer();\n"
     "    final notifier = container.read(myProvider.notifier);\n"
     "  Production code must accept Ref, not ProviderContainer, in constructors."),

    (r"Fields can't be initialized in both the parameter list and the initializers|"
     r"field_initialized_in_parameter_and_initializer",
     "⚠️  DOUBLE INITIALISATION: A field is set in both 'this.field' AND the initializer list.\n"
     "  WRONG: MyClass(this.health) : health = 10;\n"
     "  RIGHT — use one or the other:\n"
     "    MyClass(this.health);               // from parameter\n"
     "    MyClass(int h) : health = h;        // from initializer\n"
     "    MyClass({this.health = 10});        // named with default"),

    (r"Instance member '(\w+)' can't be accessed using static access|"
     r"can't be accessed using static access",
     "⚠️  STATIC VS INSTANCE: You called an instance method as if it were static.\n"
     "  WRONG: NovaProductionModifier.apply(star, rate)\n"
     "  RIGHT: NovaProductionModifier().apply(star, rate)\n"
     "  Or: final modifier = NovaProductionModifier(); modifier.apply(star, rate);\n"
     "  If it should be static, declare it: static double apply(...)"),

    (r"Target of URI doesn't exist: '\.\.\/|"
     r"Target of URI doesn't exist: '\.\/|"
     r"doesn't exist.*'\.\.\/.*\.dart'|"
     r"doesn't exist.*'\.\/.*\.dart'",
     "⚠️  RELATIVE IMPORT PATH: Relative imports ('../' or './') don't resolve reliably.\n"
     "  ALWAYS use package-absolute imports:\n"
     "    WRONG: import '../beacon_speed_modifier.dart';\n"
     "    WRONG: import './unit_formation_commander.dart';\n"
     "    RIGHT: import 'package:astro_flux/game/beacon_speed_modifier.dart';\n"
     "    RIGHT: import 'package:astro_flux/game/unit_formation_commander.dart';\n"
     "  Pattern: lib/game/foo.dart → package:astro_flux/game/foo.dart\n"
     "  Never use paths starting with ./ or ../"),

    # ── New entries added 2026-05-30 round 5 ────────────────────────────────

    (r"package:flame/gestures\.dart|flame/gestures",
     "⚠️  FLAME GESTURES IMPORT: 'package:flame/gestures.dart' does not exist in Flame 1.x.\n"
     "  Gesture/event handling moved to:\n"
     "    import 'package:flame/events.dart';  // TapCallbacks, DragCallbacks, etc.\n"
     "  Replace any 'package:flame/gestures.dart' import with 'package:flame/events.dart'."),

    (r"method 'Random' isn't defined|'Random' isn't defined for the type|"
     r"The class 'Random' isn't defined",
     "⚠️  RANDOM MISSING IMPORT: Random is in dart:math — it is not auto-imported.\n"
     "  Add to the top of the file:\n"
     "    import 'dart:math';\n"
     "  Then use: final rng = Random(); final value = rng.nextInt(100);"),

    (r"positional arguments expected by 'TapDownEvent\.new'|"
     r"TapDownEvent\.new.*positional argument",
     "⚠️  TAPDOWNEVENT IS INTERNAL: Never instantiate TapDownEvent, DragStartEvent, "
     "or other Flame event classes directly in tests — they require internal Flame state.\n"
     "  Instead, test tap behaviour by using testWithFlameGame and the component's "
     "onTapDown method indirectly, or mock the callback:\n"
     "    testWithFlameGame('tap test', (game) async {\n"
     "      final component = MyComponent();\n"
     "      await game.ensureAdd(component);\n"
     "      // Call the handler directly if testing logic, not the event:\n"
     "      component.handleTap(Vector2(100, 100));\n"
     "    });\n"
     "  Do NOT call TapDownEvent(), DragStartEvent(), or ScaleStartInfo() in tests."),

    (r"'Component\.key'.*isn't a valid.*'Widget\.key'|"
     r"Component.*Widget.*key.*override|Widget.*Component.*key.*conflict",
     "⚠️  FLAME/FLUTTER KEY CONFLICT: A class is trying to extend or implement both "
     "a Flame Component and a Flutter Widget simultaneously — this is not supported.\n"
     "  Flame components (PositionComponent, etc.) and Flutter widgets (StatelessWidget, etc.) "
     "have incompatible 'key' properties and cannot be combined in one class.\n"
     "  Use composition instead:\n"
     "    - Game canvas content → Flame Component\n"
     "    - UI overlays → Flutter Widget added via game.overlays\n"
     "  NEVER write: class Foo extends PositionComponent implements Widget"),

    (r"Undefined name 'gameServiceProvider'|gameServiceProvider.*isn't defined",
     "⚠️  gameServiceProvider IMPORT: gameServiceProvider is declared in lib/game/game_service.dart.\n"
     "  Correct import:\n"
     "    import 'package:astro_flux/game/game_service.dart';\n"
     "  OR use the re-export shim (works for test files):\n"
     "    import 'package:astro_flux/models/game_state_provider.dart';\n"
     "  NEVER redefine gameServiceProvider or GameStateNotifier in a new file — they already exist.\n"
     "  NEVER write stub implementations of GameState, Mote, Vector, or Star — use the real models:\n"
     "    import 'package:astro_flux/models/game_state.dart';   // GameState\n"
     "    import 'package:astro_flux/models/mote.dart';         // Mote\n"
     "    import 'package:astro_flux/models/vector.dart';       // Vector\n"
     "    import 'package:astro_flux/models/star.dart';         // Star"),

    # ── New entries added 2026-05-30 (learned from run analysis) ─────────────

    (r"mixin_of_non_class|Classes can only mix in mixins and classes",
     "⚠️  MIXIN ERROR: You wrote 'with SomeClass' where SomeClass is NOT declared as a mixin.\n"
     "  The 'with' keyword only works for classes declared as 'mixin' or 'mixin class'.\n"
     "  CORRECT:  mixin Foo on PositionComponent { ... }  then  class Bar extends PositionComponent with Foo\n"
     "  WRONG:    class Bar extends PositionComponent with SomeOtherClass\n"
     "  Common offenders — these are CLASSES, not mixins (do NOT use with):\n"
     "    HasGameRef, Component, PositionComponent, FlameGame, AstroGame, any Notifier subclass.\n"
     "  To share behaviour, use inheritance (extends) or composition, not with."),

    (r"value of type '(\w+)\?' can't be assigned to.*'(\w+)'[^?]|"
     r"type '(\w+)\?' is not a subtype of type '(\w+)'",
     "⚠️  NULL SAFETY: A nullable type (?) is being assigned to a non-nullable variable.\n"
     "  Fix options (choose the right one for context):\n"
     "    1. Null-check before use:   if (value != null) { useIt(value); }\n"
     "    2. Assert non-null:         useIt(value!);\n"
     "    3. Provide fallback:        useIt(value ?? defaultValue);\n"
     "    4. Make the target nullable: SomeType? myVar;\n"
     "  NEVER silence this with a cast — the null-safety system is correct.\n"
     "  Common pattern: firstWhere() returns T? — always handle the null case."),

    # Vector2 .dx / .dy — those are Offset properties, not Vector2
    (r'\.dx\b|\.dy\b',
     "WRONG PROPERTY: Vector2 does NOT have .dx or .dy — those belong to Offset (dart:ui).\n"
     "  Vector2 uses .x and .y.\n"
     "  WRONG: position.dx, position.dy\n"
     "  RIGHT: position.x,  position.y\n"
     "  If you need an Offset from a Vector2: Offset(v.x, v.y) or v.toOffset()"),

    # GestureHandler / any game-layer class storing WidgetRef typed as Ref
    (r'final\s+(?:Ref|WidgetRef)\s+ref\s*;.*GestureHandler|GestureHandler[^{]*final\s+(?:Ref|WidgetRef)\s+ref',
     "WRONG TYPE: GestureHandler.ref must be declared as 'final Ref ref' (Riverpod Ref, not WidgetRef).\n"
     "  GestureHandler is constructed inside a Provider, so it receives a plain Ref — not WidgetRef.\n"
     "  WRONG: final WidgetRef ref;\n"
     "  RIGHT: final Ref ref;\n"
     "  Import: import 'package:flutter_riverpod/flutter_riverpod.dart'; (Ref is in there)"),

    (r"argument type 'Vector2' can't be assigned to the parameter type 'Offset'|"
     r"type 'Vector2' is not a subtype of type 'Offset'",
     "⚠️  Vector2 IS NOT Offset — they are incompatible types.\n"
     "  Convert manually wherever an Offset is required:\n"
     "    WRONG: someMethod(myVector2)          // type error\n"
     "    WRONG: someMethod(myVector2.toOffset())  // .toOffset() does NOT exist on Vector2\n"
     "    RIGHT: someMethod(Offset(myVector2.x, myVector2.y))\n"
     "  Common places this fires:\n"
     "    • GestureDetector callbacks — use localPosition (Offset), not a Vector2\n"
     "    • canvas.drawLine(a, b, paint) — a and b must be Offset\n"
     "    • Transform.translate(offset:) — must be Offset\n"
     "  If you're calling a Flame API (spawnSpark, etc.), those take Vector2 — pass Vector2.\n"
     "  Rule: Flame game canvas = Vector2.  Flutter widget layer = Offset."),

    (r"argument type 'Duration' can't be assigned.*'double'|"
     r"type 'Duration' is not a subtype of type 'double'",
     "⚠️  DURATION VS DOUBLE: Flame's update loop uses double (seconds), not Duration.\n"
     "  Never pass a Duration where a double is expected.\n"
     "  WRONG: timer = Duration(seconds: 3)  then  if (elapsed > timer)\n"
     "  RIGHT: double _timer = 0;  then  _timer += dt;  if (_timer >= 3.0)\n"
     "  To convert Duration → double seconds: duration.inMilliseconds / 1000.0\n"
     "  All cooldown/timer fields in this codebase are double (seconds), never Duration."),

    (r"'currentHealth' can't be used as a setter.*final|"
     r"can't be used as a setter.*'currentHealth'|"
     r"setter.*isn't defined.*'Star'|"
     r"'hp' can't be used as a setter.*final",
     "⚠️  FINAL FIELD ON STAR/UNIT: Star and unit models use final fields — you cannot assign to them.\n"
     "  Star.currentHealth, Star.hp, Star.ownerId are all FINAL — never write star.currentHealth = x.\n"
     "  To update a Star, use the notifier:\n"
     "    ref.read(gameServiceProvider.notifier).updateStar(starId, health: newHealth);\n"
     "  Or replace the whole Star via copyWith if it supports it:\n"
     "    final updated = star.copyWith(currentHealth: newHealth);\n"
     "  NEVER mutate model fields directly — the models are immutable value objects."),

    (r"Undefined class 'Star'.*enemy_ai|enemy_ai.*Undefined class 'Star'|"
     r"Undefined class 'PositionComponent'.*enemy_ai|enemy_ai.*Undefined class 'PositionComponent'|"
     r"Undefined name 'Vector2'.*enemy_ai|enemy_ai.*Undefined name 'Vector2'",
     "⚠️  MISSING IMPORTS in enemy_ai.dart. Add these at the top:\n"
     "    import 'package:flame/components.dart' hide Vector;\n"
     "    import 'package:astro_flux/models/star.dart';\n"
     "    import 'package:astro_flux/models/vector.dart';\n"
     "    import 'package:astro_flux/models/game_state_provider.dart';\n"
     "  AiStrategy is a local enum — define it IN enemy_ai.dart if it doesn't exist yet:\n"
     "    enum AiStrategy { expander, builder, defender }\n"
     "  Do NOT import PositionComponent for data — it's a Flame render class, not a model."),

    (r"getter 'position' isn't defined.*'Star'|"
     r"'Star'.*getter 'position'.*isn't defined|"
     r"getter 'position' isn't defined.*'Vector'[^C]|"
     r"'Vector'.*getter 'position'.*isn't defined",
     "⚠️  Star AND Vector are PURE DATA MODELS — they have NO position field.\n"
     "  Star fields: id (int), ownerId (String), health (int), maxHealth (int), productionRate (int).\n"
     "  Vector fields: id (int), owner (String), tier (int).\n"
     "  Position is stored SEPARATELY in AstroGame._starPositions (Map<int, Vector2>).\n"
     "  To get a star's world position in game code:\n"
     "    final pos = _starPositions[star.id];  // Vector2 or null\n"
     "  To get a unit's world position, use VectorComponent.position (the Flame component).\n"
     "  NEVER write: star.position, star.worldPosition, vector.position, unit.position\n"
     "  These fields do NOT exist on the model classes."),

    (r"enum_without_constants|The enum must have at least one constant",
     "⚠️  EMPTY ENUM: An enum was declared with no constants — this is a Dart error.\n"
     "  For AiStrategy, always define it with all variants:\n"
     "    enum AiStrategy { expander, builder, defender, aggressor }\n"
     "  Never declare an enum body with zero values:\n"
     "    enum AiStrategy {}  // ← invalid Dart\n"
     "  Define the enum BEFORE the class that uses it in the same file."),

    (r"Undefined name 'difficultyProvider'|difficultyProvider.*isn't defined|"
     r"Undefined name 'settingsProvider'.*enemy_ai|"
     r"Undefined name 'gameRulesProvider'",
     "⚠️  HALLUCINATED PROVIDER: difficultyProvider, settingsProvider, and gameRulesProvider "
     "do NOT exist in this project.\n"
     "  Do not reference providers that aren't in the provided file list.\n"
     "  For difficulty/balance constants, use the constants in lib/game/balance.dart directly:\n"
     "    import 'package:astro_flux/game/balance.dart';\n"
     "    // e.g. Balance.enemyTickInterval, Balance.maxEnemyUnits\n"
     "  Never invent a provider — only use providers that exist in the codebase."),

    (r"positional argument.*AstroGame|AstroGame.*positional argument|"
     r"argument.*expected by 'AstroGame\.new'",
     "⚠️  AstroGame CONSTRUCTOR: AstroGame requires exactly TWO positional arguments: a Riverpod Ref and a LevelDefinition.\n"
     "  The class signature is:\n"
     "    class AstroGame extends FlameGame {\n"
     "      AstroGame(this._ref, this._level);  // Ref, LevelDefinition\n"
     "    }\n"
     "  CORRECT:\n"
     "    final game = AstroGame(ref, levelDefinition);\n"
     "  In tests, do NOT construct AstroGame directly — mock it or use a fake:\n"
     "    final mockGame = MockAstroGame();\n"
     "    // or: final game = AstroGame(container.read, LevelDefinition.defaults());\n"
     "  WRONG: AstroGame()         ← missing both args\n"
     "  WRONG: AstroGame(ref)      ← missing LevelDefinition\n"
     "  Note: AstroGame does NOT use ScaleDetector — pinch-to-zoom is handled in main.dart via Listener."),

    (r"MoteComponent|'MoteComponent' isn't defined|function 'MoteComponent'",
     "⚠️  MoteComponent DOES NOT EXIST. The correct class is UnitComponent (in lib/components/mote_component.dart).\n"
     "  The model class is Unit (not Mote) from lib/models/mote.dart.\n"
     "  Correct constructor:\n"
     "    UnitComponent(\n"
     "      unit: Unit(id: someInt),\n"
     "      glowRadius: 12.0,\n"
     "      neonColor: someColor,\n"
     "      owner: 'player',   // optional, defaults to 'player'\n"
     "      position: Vector2(x, y),  // optional\n"
     "    )\n"
     "  NEVER use MoteComponent, NEVER use Mote — these names do not exist in this codebase."),

    # ── Flame DragCallbacks / TapCallbacks event API ──────────────────────────
    (r"argument type 'Vector2' can't be assigned to the parameter type 'Offset'.*gesture_handler|"
     r"gesture_handler.*argument type 'Vector2'.*Offset|"
     r"DragUpdateDetails.*Vector2|Vector2.*DragUpdateDetails|"
     r"TapDownDetails.*Vector2|Vector2.*TapDownDetails",
     "⚠️  FLAME EVENT API: You are mixing Flutter's raw gesture API (DragUpdateDetails, TapDownDetails)\n"
     "  with Flame's DragCallbacks/TapCallbacks. These are incompatible.\n"
     "\n"
     "  Flame 1.x DragCallbacks (import 'package:flame/events.dart') uses:\n"
     "    onDragStart(DragStartEvent event)  → event.localStartPosition (Vector2)\n"
     "    onDragUpdate(DragUpdateEvent event) → event.localPosition (Vector2), event.delta (Vector2)\n"
     "    onDragEnd(DragEndEvent event)\n"
     "    onDragCancel(DragCancelEvent event)\n"
     "\n"
     "  Flame 1.x TapCallbacks uses:\n"
     "    onTapDown(TapDownEvent event)  → event.localPosition (Vector2)\n"
     "    onTapUp(TapUpEvent event)      → event.localPosition (Vector2)\n"
     "\n"
     "  ALL positions from Flame events are already Vector2 — NEVER convert to/from Offset.\n"
     "  Flutter's gesture.DragUpdateDetails.localPosition is Offset — NEVER use it in Flame code.\n"
     "\n"
     "  GestureHandler signature (already implemented and locked — do NOT rewrite this file):\n"
     "    class GestureHandler extends PositionComponent with HasGameRef, TapCallbacks, DragCallbacks\n"
     "    GestureHandler({required Ref ref, required Map<int, Vector2> starPositions,\n"
     "                    required void Function(int) onStarTapped, required Vector2 size})\n"
     "  Callbacks set after construction:\n"
     "    gh.onTapWorld, gh.onCircleSelect, gh.onDoubleTapStar, gh.onTapEmpty"),
]

