import '../products/product_interface.dart';

class ShoppingCart {
  final List<Map<String, dynamic>> _items = [];

  List<Map<String, dynamic>> get items => List.unmodifiable(_items);

  void addItem(IProduct product, int quantity) {
    if (product.id.isNotEmpty && quantity > 0) {
      _items.add({
        'product': product,
        'quantity': quantity,
      });
    }
  }
}