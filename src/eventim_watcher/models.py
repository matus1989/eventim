"""Model domenowy dostepnosci biletow.

Wartosci ``availability`` pochodza ze ``schema.org/AggregateOffer`` i sa
przechowywane **bezposrednio z URI**, np. ``https://schema.org/SoldOut``
na wejsciu daje ``"SoldOut"``.

Regula interpretacji (ADR-5, docs/architecture.md):

* ``"SoldOut"``, ``"Discontinued"``, ``"OutOfStock"``  -> niedostępne
* **jakakolwiek inna obecna wartosc**                    -> dostepne

Czarna lista zamiast bialej, bo schema.org regularnie dodaje nowe stany
(``PreSale``, ``LimitedAvailability``, ``OnlineOnly``, ...) i biała lista
po cichu przegapilaby alert.

Rozroznienie dwóch przypadków, ktore wygladaja podobnie, ale znacza
zupełnie co innego:

* **obecna, nieznana wartosc** (``PreSale``) -> dostepne, zgodnie z ADR-5;
* **brak pola** -> ``UNKNOWN``, ktore **nie** uruchamia alertu o biletach,
  ale jest raportowane osobno (:attr:`Series.has_unknown`).

Roznica jest istotna: gdy Eventim zmieni markup, brak pola oznacza
niepewnosc. Traktowanie tego jako "dostepne" zasypaloby uzytkownika
alarmami o biletach, ktorych nie ma; traktowanie tego jako cichego
"wyprzedane" zamaskowalo by awarie. Stąd osobny stan do raportowania.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "UNAVAILABLE",
    "UNKNOWN",
    "Series",
    "Term",
    "available_label",
]

#: Stany schema.org oznaczajace brak dostepnych biletow.
UNAVAILABLE = frozenset({"SoldOut", "Discontinued", "OutOfStock"})

#: Zastepcza wartosc, gdy pole ``availability`` nie występuje w danych.
UNKNOWN = "Unknown"

#: Polskie opisy stanow - komunikaty Telegram maja byc po polsku.
_LABELS = {
    "SoldOut": "wyprzedane",
    "InStock": "dostepne",
    "LimitedAvailability": "ostatnie sztuki",
    "PreOrder": "przedsprzedaz",
    "PreSale": "sprzedaz wstepna",
    "BackOrder": "na zamowienie",
    "Discontinued": "wylonietowane",
    "OutOfStock": "brak w magazynie",
    "OnlineOnly": "tylko online",
    UNKNOWN: "stan nieznany",
}


def available_label(availability: str) -> str:
    """Polska etykieta stanu dostępności (z bezpiecznym fallbackiem)."""
    return _LABELS.get(availability, availability)


@dataclass(frozen=True)
class Series:
    """Seria wydarzeń wraz z terminami."""

    name: str
    url: str
    terms: tuple[Term, ...]

    @property
    def available_terms(self) -> tuple[Term, ...]:
        """Terminy z potwierdzona dostepnoscia."""
        return tuple(term for term in self.terms if term.available)

    @property
    def unknown_terms(self) -> tuple[Term, ...]:
        """Terminy bez pola ``availability`` - wymagaja zgloszenia, nie alertu."""
        return tuple(term for term in self.terms if term.is_unknown)

    @property
    def any_available(self) -> bool:
        """Czy da sie cos kupic. ``False`` takze gdy nic nie wiadomo - patrz modul."""
        return bool(self.available_terms)

    @property
    def has_unknown(self) -> bool:
        """Czy w danych brakuje pola dostępności (anomalia parsowania)."""
        return bool(self.unknown_terms)

    @property
    def price_range(self) -> tuple[float, float] | None:
        """Najniższa i najwyższa cena w EUR, albo ``None`` gdy brak danych."""
        prices = [
            price
            for term in self.terms
            for price in (term.low_price, term.high_price)
            if price is not None
        ]
        if not prices:
            return None
        return min(prices), max(prices)


@dataclass(frozen=True)
class Term:
    """Pojedynczy termin w serii wydarzeń."""

    name: str
    start: str
    url: str
    availability: str

    low_price: float | None = None
    high_price: float | None = None
    currency: str | None = None

    @property
    def available(self) -> bool:
        """Czy termin ma dostepne bilety.

        ``False`` dla stanu nieznanego: nieznany to niepotwierdzona dostepnosc.
        Rozroznienie od wartosci obecnej w czarnej liscie jest celowe.
        """
        if self.is_unknown:
            return False
        return self.availability not in UNAVAILABLE

    @property
    def is_unknown(self) -> bool:
        """Czy brak pola ``availability`` w danych zrodlowych."""
        return self.availability == UNKNOWN

    @property
    def label(self) -> str:
        """Polska etykieta stanu dostępności."""
        return available_label(self.availability)