class Publisher:
    platform = ""

    def publish(self, post):
        """Send an approved post live. Returns an external id or path."""
        raise NotImplementedError
