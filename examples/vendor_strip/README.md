# Vendor strip export samples

Regenerate from the sample job:

```bash
panelcad electrical strip-export examples/vendor_strip/strip_sample.pcad.yaml --strip X1 --vendor phoenix -o examples/vendor_strip/phoenix_project_complete.csv
panelcad electrical strip-export examples/vendor_strip/strip_sample.pcad.yaml --strip X1 --vendor clipx -o examples/vendor_strip/phoenix_clipx.xml
panelcad electrical strip-export examples/vendor_strip/strip_sample.pcad.yaml --strip X1 --vendor wago --format csv -o examples/vendor_strip/wago_smartdesigner.csv
panelcad electrical strip-export examples/vendor_strip/strip_sample.pcad.yaml --strip X1 --vendor wagoxml -o examples/vendor_strip/wago_smartdesigner.xml
panelcad electrical strip-export examples/vendor_strip/strip_sample.pcad.yaml --strip X1 --vendor weidmueller -o examples/vendor_strip/weidmueller_wmc.csv
```

`phoenix_clipx.xsd` and `wago_smartdesigner.xsd` are PanelCAD interchange schemas. They are not vendor-published. See `docs/VENDOR_STRIP_EXPORT.md`.
