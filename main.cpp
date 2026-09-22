#include "1.cpp"
#include <iostream>

int main(int argc, char** argv) {
    if (argc > 2) {
        std::cerr << "Usage: orders2 [path/to/.env]\n";
        return 1;
    }
    const std::filesystem::path env_path = argc == 2 ? argv[1] : ".env";
    for (;;) {
        std::cout << "\nGate orders\n"
                     "1. Query contract\n2. Query positions\n3. Set leverage\n"
                     "4. Create trailing order\n5. Stop trailing order\n"
                     "6. Query trailing order details\n0. Exit\nChoice: " << std::flush;
        std::string choice;
        if (!std::getline(std::cin, choice)) return 0;
        choice = trim(choice);
        if (choice == "0") return 0;
        if (choice.size() != 1 || choice[0] < '1' || choice[0] > '6') {
            std::cout << "Invalid choice.\n";
            continue;
        }
        try {
            // Reload explicit runtime configuration for each operation.
            const auto config = load_config(env_path);
            if (choice == "3" || choice == "4" || choice == "5") {
                std::cout << "LIVE Gate account operation: " << choice << '\n';
                if (choice == "3")
                    std::cout << "Contract: " << config.contract << ", leverage: "
                              << config.leverage << ", margin mode: " << config.margin_mode << '\n';
                else if (choice == "4")
                    std::cout << "Contract: " << config.contract << ", amount: " << config.amount
                              << ", activation price: " << config.activation_price
                              << ", is_gte: " << config.is_gte << ", price type: " << config.price_type
                              << ", offset: " << config.price_offset
                              << ", reduce only: " << config.reduce_only << ", text: " << config.text << '\n';
                else
                    std::cout << "Order ID: " << config.stop_order_id << '\n';
                std::cout << "Type YES to send: " << std::flush;
                std::string confirmation;
                if (!std::getline(std::cin, confirmation)) return 0;
                if (confirmation != "YES") {
                    std::cout << "Cancelled.\n";
                    continue;
                }
            }
            std::string response;
            switch (choice[0]) {
                case '1': response = get_contract(config); break;
                case '2': response = get_positions(config); break;
                case '3': response = set_leverage(config); break;
                case '4': response = create_trailing_order(config); break;
                case '5': response = stop_trailing_order(config); break;
                case '6': response = get_trailing_order_detail(config); break;
            }
            std::cout << response << '\n';
        } catch (const std::exception& error) {
            std::cerr << "Error: " << error.what() << '\n';
        }
    }
}
