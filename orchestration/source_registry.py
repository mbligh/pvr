"""Supported recording-indexer coverage for canonical episode series.

Fixture scheduling (import_rugby/import_f1) is independent of this mapping.
A series absent from this registry is deliberately *not* polled.
"""
SOURCE_SERIES = {
    "bbc": ("Rugby_Womens_PWR",),
    "youtube": ("Rugby_Womens_PWR",),
    "rugbypass": (
        "Rugby_Womens_PWR",
        "Rugby_Mens_Intl",
        "Rugby_Womens_Intl",
        "Rugby_Mens_Nations",
        "Rugby_Womens_WXV",
    ),
    "rugby24": ("Rugby_Mens_Prem",),
    "reddit-teletz": ("Formula_1",),
}
RUGBY_SOURCES = ("bbc", "youtube", "rugbypass", "rugby24")
F1_SOURCES = ("reddit-teletz",)
SERIES_SOURCES = {
    series: tuple(source for source, series_set in SOURCE_SERIES.items()
                  if series in series_set)
    for series in set().union(*(set(s) for s in SOURCE_SERIES.values()))
}
RUGBY_SERIES = ("Rugby_Womens_PWR", "Rugby_Mens_Intl",
                "Rugby_Womens_Intl", "Rugby_Mens_Nations", "Rugby_Womens_WXV",
                "Rugby_Mens_Prem")


def sources_for(series):
    return SERIES_SOURCES.get(series, ())


def series_for(source):
    return SOURCE_SERIES[source]
