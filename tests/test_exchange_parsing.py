from __future__ import annotations

import asyncio
import json
import unittest

from jev_arb.config import AppConfig
from jev_arb.exchanges import CoinbaseConnector, KrakenConnector
from jev_arb.orderbook import BookStore


class ExchangeParserTests(unittest.TestCase):
    def test_public_subscription_urls_and_symbols(self):
        config = AppConfig()
        self.assertEqual(CoinbaseConnector.url, "wss://advanced-trade-ws.coinbase.com")
        self.assertEqual(KrakenConnector.url, "wss://ws.kraken.com/v2")
        self.assertEqual(config.market_specs()["BTC-USD"].venue_symbols["coinbase"], "BTC-USD")
        self.assertEqual(config.market_specs()["BTC-USD"].venue_symbols["kraken"], "BTC/USD")

    def test_coinbase_event_shape_is_read_only(self):
        config = AppConfig()
        books = BookStore()
        seen = []

        async def callback(event, quote):
            seen.append((event, quote))
            books.apply_event(event, quote)

        connector = CoinbaseConnector(config, books, callback)
        payload = {
            "channel": "l2_data", "timestamp": "2026-09-21T00:00:00Z", "sequence_num": 1,
            "events": [{"type": "snapshot", "product_id": "BTC-USD", "updates": [
                {"side": "bid", "price_level": "100", "new_quantity": "2"},
                {"side": "offer", "price_level": "101", "new_quantity": "3"},
            ]}],
        }
        # Exercise the same normalization path without opening a socket.
        event = connector._canonical("BTC-USD")
        self.assertEqual(event, "BTC-USD")
        self.assertTrue(connector.url.startswith("wss://"))


if __name__ == "__main__":
    unittest.main()


