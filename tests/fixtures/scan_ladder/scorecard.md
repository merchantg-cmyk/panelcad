# Scan ladder

Demo prints only. Scores are recall of the printed devices, wire numbers, title text, and pin-to-pin connections. A `?` is an unreadable mark, not a match and not a guess.

OCR before is the previous artwork (seal-in, DOL enclosure, vendor strip). OCR after is this pass on the drafter-style sheets. The two columns are different drawings. Vision is the existing vision-model import when a backend can run.

Vision: No vision backend can run on this machine. XAI_API_KEY is unset, Ollama is not installed, and the Grok CLI is not installed.

| source | variant | devices OCR before | devices OCR after | wires OCR before | wires OCR after | conn OCR before | conn OCR after | devices vision | wires vision | conn vision | ANSI after |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| schematic | clean_300 | 62.5% | 77.8% | 14.3% | 50.0% | 14.3% | 23.1% | unavailable | unavailable | unavailable | 0 |
| schematic | dpi_200 | 75.0% | 72.2% | 28.6% | 37.5% | 42.9% | 30.8% | unavailable | unavailable | unavailable | 0 |
| schematic | dpi_150 | 50.0% | 55.6% | 0.0% | 37.5% | 14.3% | 0.0% | unavailable | unavailable | unavailable | 0 |
| schematic | dpi_100 | 0.0% | 77.8% | 0.0% | 56.2% | 0.0% | 30.8% | unavailable | unavailable | unavailable | 0 |
| schematic | jpeg | 75.0% | 72.2% | 0.0% | 37.5% | 14.3% | 30.8% | unavailable | unavailable | unavailable | 0 |
| schematic | skew | 75.0% | 77.8% | 14.3% | 43.8% | 28.6% | 38.5% | unavailable | unavailable | unavailable | 0 |
| schematic | phone | 75.0% | 77.8% | 28.6% | 50.0% | 28.6% | 7.7% | unavailable | unavailable | unavailable | 0 |
| schematic | faded | 50.0% | 61.1% | 0.0% | 37.5% | 14.3% | 15.4% | unavailable | unavailable | unavailable | 0 |
| schematic | speckle | 50.0% | 61.1% | 0.0% | 31.2% | 14.3% | 15.4% | unavailable | unavailable | unavailable | 0 |
| schematic | fax | 62.5% | 66.7% | 14.3% | 31.2% | 14.3% | 15.4% | unavailable | unavailable | unavailable | 0 |
| panel | clean_300 | 87.5% | 55.6% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | dpi_200 | 87.5% | 55.6% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | dpi_150 | 75.0% | 0.0% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | dpi_100 | 0.0% | 0.0% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | jpeg | 62.5% | 0.0% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | skew | 87.5% | 0.0% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | phone | 62.5% | 22.2% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | faded | 87.5% | 55.6% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | speckle | 75.0% | 0.0% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| panel | fax | 100.0% | 0.0% |  |  |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | clean_300 | 0.0% | 100.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | dpi_200 | 0.0% | 100.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | dpi_150 | 0.0% | 100.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | dpi_100 | 0.0% | 100.0% | 0.0% | 0.0% | 0.0% |  | unavailable | unavailable | unavailable | 0 |
| terminal | jpeg | 0.0% | 100.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | skew | 0.0% | 100.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | phone | 0.0% | 100.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | faded | 0.0% | 0.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | speckle | 0.0% | 100.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |
| terminal | fax | 0.0% | 100.0% | 0.0% | 0.0% |  |  | unavailable | unavailable | unavailable | 0 |

