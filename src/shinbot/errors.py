"""Request failures the gateway reports as 404 and 409. Both remain ValueErrors for tool results."""


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass
