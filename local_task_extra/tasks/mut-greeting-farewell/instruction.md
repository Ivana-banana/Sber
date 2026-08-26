# Release hand-off marker

The deployment wrapper will proceed only when the exact completion marker exists at `/srv/bootstrap/farewell.marker`. Create that file so its entire byte content is `Ciao`. Do not add a newline or other text.
