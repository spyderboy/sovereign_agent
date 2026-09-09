class Order {
  final List<double> prices;

  Order({required this.prices});

  double calculateTotal() {
    return prices.fold(0, (sum, price) => sum + price);
  }
}