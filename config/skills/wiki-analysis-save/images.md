# Diagramme speichern

Nur für fertige Analysen mit mitgelieferten Diagrammen; keine neue Analyse oder
Werte erfinden. Grafikdateien sind genauso wie Markdown veröffentlichter Inhalt.

1. Speichere PNG oder statische SVG unter `assets/analyses/<slug>/<name>.png`
   beziehungsweise `.svg`. Namen: ASCII-Kebab-Case, keine Unterverzeichnisse.
2. Binde sie als `![Aussagekräftiger Alternativtext](/.fs/assets/analyses/<slug>/<name>.svg)`
   ein. Kein HTML, Inline-SVG, externer Bildabruf oder vorgefertigte Data-URL.
3. PNG muss statisch und dekodierbar sein. SVG braucht den Namespace
   `http://www.w3.org/2000/svg` und einen positiven `viewBox`. Unterstützt werden
   Formen, Pfade, Gruppen, Text, lokale Ausschnitte, Schraffuren, Gradienten,
   Marker und lokale `use`-Referenzen. Verwende Darstellungsattribute oder einfache
   `style`-Deklarationen, keine Stylesheets, Metadaten-Namespaces, Fremdbilder,
   Skripte, Animationen oder externen Ressourcen. Referenzen dürfen nur auf IDs
   derselben SVG zeigen. Nicht unterstützte Konstrukte sind ein Fehler; entferne
   keine bedeutungstragenden Diagrammteile als stillen Ersatz.
4. Beachte Grenzen: PNG 10 MiB/40 Megapixel, SVG 2 MiB/10.000 Elemente,
   maximal 50 Bildeinbindungen und 50 MiB fertiges HTML. SVG-Dimensionen sind
   ebenfalls begrenzt; der Renderer nennt verletzte Struktur-/Wertregeln.
5. Führe `render-analysis` aus. Prüfe die gemeldete PNG-/SVG-Anzahl, sichtbare
   Diagramme und lesbare Werte/Quellen. Erhalte eine zugängliche Datentabelle,
   wenn sie Teil der Analyse ist. Keine erfundene Interaktivität behaupten:
   eingebettete Diagramme sind statische Bilder.
6. Committe Diagrammassets zusammen mit Analyse und HTML. Bei einem Renderfehler
   bleibt eine vorherige Druckansicht erhalten, ist aber kein aktueller Erfolg.
