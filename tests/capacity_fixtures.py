"""Empty, explicitly complete offer pages for unrelated offline tests."""


class EmptyOffersManager:
    def __init__(self, client, category_id=None):
        self.client = client
        self.category_id = category_id

    def list_offers(self):
        category_id = self.category_id or self.client.category_id
        return {"items": [], "complete": True, "category": {"id": str(category_id)}, "currency": "RUB"}

    def connection_metadata(self):
        return {"currency": "RUB", "category": self.list_offers()["category"], "count": 0}
