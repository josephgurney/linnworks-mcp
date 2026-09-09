# Dangling GLT templates — SWH Shopify (all configurators)

**Swept 9 Sep 2026.** 31,841 active Linnworks items -> 11,351 GLT templates across 34
configurators (ChannelId 18) -> 11,012 distinct stored `ActiveListingId`s.

## Result

| | |
|---|---|
| GLT templates on SWH Shopify | 11351 |
| **Templates whose stored listing is DELETED on Shopify (dangling)** | **145** |
| Distinct deleted listing ids | 145 |

## The signal worth knowing

**Every one of the 141 templates whose status reads "Not deleted" is dangling** — a perfect
correlation in this sweep. That status is Linnworks recording that it tried to remove a
listing which was already gone. It is the cheapest possible flag for this fault.

By contrast, of the 745 templates reading "Errors while updating", only
4 are dangling — so that status is NOT a reliable indicator; most of those
point at products that still exist (commonly ARCHIVED or DRAFT in Shopify).

## Dangling templates by configurator

| Configurator | Dangling |
|---|---|
| Master - Size | 60 |
| Skates - Parts & Hardware - (Size) | 31 |
| Skateboards - Decks - (Deck-size) THIS ONE | 18 |
| Skateboards - Completes - (Deck width) | 12 |
| Skates - Parts - Wheels - (Wheels Size) | 8 |
| Skates - Quads/inline/Ice/Heelys - (Size) | 5 |
| Clothing - (Size) | 4 |
| Skateboards - Parts - Trucks - (Width) | 4 |
| Skateboards - Protection - (Pad Size) | 3 |

## Full list

| Template | SKU | Dead listing | Status | Snapshot |
|---|---|---|---|---|
| 36222 | `Ace-cexrte'ee-al` | 9173751333110 | Not deleted | 2025-10-02 |
| 36220 | `Ace-cexrte'ee-sh` | 9173750939894 | Not deleted | 2025-10-02 |
| 27814 | `Theories-esOfismmryeeck` | 8978958385398 | Not deleted | 2025-03-06 |
| 36224 | `Theories-esOfisryeeck` | 9173756084470 | Not deleted | 2025-10-02 |
| 45490 | `1002079509-vtk-c-rawBLT` | 9437909057782 | Not deleted | 2026-07-01 |
| 45487 | `10021008808-vtk-c-rawBLT` | 9437908926710 | Not deleted | 2026-07-01 |
| 45489 | `10021932912-vtk-c-rawBLT` | 9437909025014 | Not deleted | 2026-07-01 |
| 45488 | `1002398208-vtk-c-rawBLT` | 9437908959478 | Not deleted | 2026-07-01 |
| 48553 | `50320007A00` | 9438363484406 | Not deleted | 2026-07-01 |
| 48560 | `50910001F12M` | 9438363943158 | Not deleted | 2026-07-01 |
| 48561 | `50910001F18M` | 9438363975926 | Not deleted | 2026-07-01 |
| 48562 | `50910001F24M` | 9438364041462 | Not deleted | 2026-07-01 |
| 48559 | `50910001I15` | 9438363910390 | Not deleted | 2026-07-01 |
| 48556 | `5202304908` | 9438363713782 | Not deleted | 2026-07-01 |
| 48557 | `5202304909` | 9438363812086 | Not deleted | 2026-07-01 |
| 48558 | `5202304910` | 9438363877622 | Not deleted | 2026-07-01 |
| 48552 | `67010124B00` | 9438363451638 | Not deleted | 2026-07-01 |
| 48566 | `67020062A00` | 9438364172534 | Not deleted | 2026-07-01 |
| 46954 | `ABR-COM-3625` | 9438230708470 | Not deleted | 2026-07-01 |
| 46956 | `ABR-SKD-5010` | 9438230839542 | Not deleted | 2026-07-01 |
| 50301 | `Ace-ceF1ckedW*60` | 9439714902262 | Not deleted | 2026-07-03 |
| 47071 | `C1042013058` | 9438243291382 | Not deleted | 2026-07-03 |
| 48603 | `G-TOOL-BLK` | 9438365778166 | Not deleted | 2026-07-01 |
| 48578 | `GT-1` | 9438364860662 | Not deleted | 2026-07-01 |
| 48567 | `GT-11` | 9438364238070 | Not deleted | 2026-07-01 |
| 48565 | `GT-12` | 9438364139766 | Not deleted | 2026-07-01 |
| 48574 | `GT-2` | 9438364729590 | Not deleted | 2026-07-01 |
| 48573 | `GT-3` | 9438364696822 | Not deleted | 2026-07-01 |
| 48572 | `GT-4` | 9438364631286 | Not deleted | 2026-07-01 |
| 48570 | `GT-6` | 9438364369142 | Not deleted | 2026-07-01 |
| 48569 | `GT-7` | 9438364336374 | Not deleted | 2026-07-01 |
| 48568 | `GT-8` | 9438364270838 | Not deleted | 2026-07-01 |
| 47062 | `HWC7SPRING` | 9438242767094 | Not deleted | 2026-07-03 |
| 48596 | `JES-BLK-ROLL11` | 9438365548790 | Not deleted | 2026-07-01 |
| 48595 | `JES-BLK-ROLL12` | 9438365516022 | Not deleted | 2026-07-01 |
| 48588 | `JES-GRP-0041` | 9438365221110 | Not deleted | 2026-07-01 |
| 48594 | `JES-ROAM-ROLL11` | 9438365483254 | Not deleted | 2026-07-01 |
| 48589 | `JES-SHEET-9-FGRN` | 9438365253878 | Not deleted | 2026-07-01 |
| 48585 | `JES-SHEET-9-MIDBLUE` | 9438365122806 | Not deleted | 2026-07-01 |
| 48583 | `JES-SHEET-9-NGRN` | 9438365024502 | Not deleted | 2026-07-01 |
| 48580 | `JES-SHEET-9-NPNK` | 9438364926198 | Not deleted | 2026-07-01 |
| 48581 | `JES-SHEET-9-NYEL` | 9438364958966 | Not deleted | 2026-07-01 |
| 48586 | `JES-SHEET-9-ORANGE` | 9438365155574 | Not deleted | 2026-07-01 |
| 48592 | `JES-SHEET-9-PANRED` | 9438365417718 | Not deleted | 2026-07-01 |
| 48587 | `JES-SHEET-9-PUR` | 9438365188342 | Not deleted | 2026-07-01 |
| 48584 | `JES-SHEET-9-RED` | 9438365090038 | Not deleted | 2026-07-01 |
| 48591 | `JES-SHEET-9-SBYEL` | 9438365384950 | Not deleted | 2026-07-01 |
| 48582 | `JES-SHEET-9-SKYBLUE` | 9438364991734 | Not deleted | 2026-07-01 |
| 48590 | `JES-SHEET-9-SWGRY` | 9438365319414 | Not deleted | 2026-07-01 |
| 48593 | `JES-SHEET-9-WHITE` | 9438365450486 | Not deleted | 2026-07-01 |
| 48577 | `JES-ULTRACLR-9-SHEET` | 9438364827894 | Not deleted | 2026-07-01 |
| 50930 | `LS126CONELTBLKBLK-8` | 9482463510774 | Not deleted | 2026-07-28 |
| 50931 | `LS126CONELTBLKBLK-9` | 9482463543542 | Not deleted | 2026-07-28 |
| 48597 | `MOD-3TOOLUTILITY-PINK` | 9438365581558 | Not deleted | 2026-07-01 |
| 48575 | `MOD-3TOOLUTILITY-PPL` | 9438364762358 | Not deleted | 2026-07-01 |
| 45492 | `PSC-SP25-100-85-vtk-c-rawBLT` | 9437909254390 | Not deleted | 2026-07-01 |
| 47077 | `R1031634047` | 9438243815670 | Not deleted | 2026-07-01 |
| 48599 | `RIP-SHOEREPAIRGLUE-BLK` | 9438365647094 | Not deleted | 2026-07-01 |
| 48564 | `SC-W25-24` | 9438364106998 | Not deleted | 2026-07-01 |
| 48563 | `SC-W25-25` | 9438364074230 | Not deleted | 2026-07-01 |
| 49272 | `SFR055LILYPAD` | 9439487492342 | Not deleted | 2026-07-03 |
| 50336 | `SL401901200A8750` | 9439717949686 | Not deleted | 2026-07-03 |
| 48598 | `SS-YT-BLK` | 9438365614326 | Not deleted | 2026-07-01 |
| 49113 | `pro-tec-pads-street-adult--543a4-CHECKER` | 9438622318838 | Not deleted | 2026-07-02 |
| 27081 | `CDTHEBARLOGKA8.25-vtk-c-rawBLT` | 8966847693046 | Not deleted | 2025-02-13 |
| 32818 | `Powell-llll&rd•teen•pe91•75x2865ch-vtk-c-rawBLT` | 9100871860470 | Not deleted | 2025-06-23 |
| 32821 | `Powell-lltaabanis•vy•75x2865-vtk-c-rawBLT` | 9100871958774 | Not deleted | 2025-06-23 |
| 32819 | `Powell-lltaabseon•ow•75x2865-vtk-c-rawBLT` | 9100871893238 | Not deleted | 2025-06-23 |
| 32820 | `Powell-lltaeder•ht•80x3145-vtk-c-rawBLT` | 9100871926006 | Not deleted | 2025-06-23 |
| 32806 | `Powell-lltaer•enined•80x3145-vtk-c-rawBLT` | 9100871434486 | Not deleted | 2025-06-23 |
| 32815 | `Powell-lltaer•owined•775x318-vtk-c-rawBLT` | 9100871762166 | Not deleted | 2025-06-23 |
| 32816 | `Powell-lltaer•ueined•75x3070-vtk-c-rawBLT` | 9100871794934 | Not deleted | 2025-06-23 |
| 32827 | `Powell-lltall&rd•ue•75x2865-vtk-c-rawBLT` | 9100872188150 | Not deleted | 2025-06-23 |
| 32808 | `Powell-lltalluedepe47-80x3145-vtk-c-rawBLT` | 9100871500022 | Not deleted | 2025-06-23 |
| 32810 | `Powell-lltatots•ckow•80x3145-vtk-c-rawBLT` | 9100871565558 | Not deleted | 2025-06-23 |
| 39433 | `ZDREAP8-vtk-c-rawBLT` | 9304682823926 | Not deleted | 2026-03-10 |
| 41633 | `304006-000-825` | 9414011519222 | Not deleted | 2026-06-12 |
| 41663 | `304017-000-800` | 9414013223158 | Not deleted | 2026-06-12 |
| 41642 | `304022-000-775` | 9414011912438 | Not deleted | 2026-06-12 |
| 41634 | `304025-000-775` | 9414011551990 | Not deleted | 2026-06-12 |
| 41664 | `304032-000-800` | 9414013255926 | Not deleted | 2026-06-12 |
| 41666 | `304035-000-825` | 9414013354230 | Not deleted | 2026-06-12 |
| 41652 | `304037-000-825` | 9414012305654 | Not deleted | 2026-06-12 |
| 41653 | `304037-000-850` | 9414012338422 | Not deleted | 2026-06-12 |
| 41656 | `304051-000-825` | 9414012436726 | Not deleted | 2026-06-12 |
| 41657 | `304051-000-832` | 9414012469494 | Not deleted | 2026-06-12 |
| 41650 | `304053-000-825` | 9414012207350 | Not deleted | 2026-06-12 |
| 41645 | `304054-000-080` | 9414012010742 | Not deleted | 2026-06-12 |
| 41648 | `304054-000-880` | 9414012109046 | Not deleted | 2026-06-12 |
| 41612 | `304059-000-825` | 9414010568950 | Not deleted | 2026-06-12 |
| 41615 | `304059-000-860` | 9414010732790 | Not deleted | 2026-06-12 |
| 41616 | `304059-000-875` | 9414010765558 | Not deleted | 2026-06-12 |
| 41621 | `304060-003-850` | 9414010994934 | Not deleted | 2026-06-12 |
| 41622 | `304060-003-860` | 9414011027702 | Not deleted | 2026-06-12 |
| 38539 | `300419-058-TH1` | 9269396930806 | Not deleted | 2026-02-03 |
| 38540 | `300419-058-TH2` | 9269396963574 | Not deleted | 2026-02-03 |
| 18902 | `MIN-SKT-0035` | 8379042431222 | Not deleted | 2024-02-28 |
| 18921 | `MIN-SKT-0036` | 8379043610870 | Not deleted | 2024-02-28 |
| 25134 | `187-87erdsr.ixcketiclt-ti` | 8913839849718 | Not deleted | 2024-12-04 |
| 48918 | `337530-000-parent` | 9438565302518 | Not deleted | 2026-07-03 |
| 37429 | `REKD-KDgyes-en` | 9213772792054 | Errors while updating | 2026-08-18 |
| 42146 | `antik-skyhawk-v2-boot-blac-10906-BLACKWHITE` | 9417863168246 | Not deleted | 2026-06-15 |
| 42152 | `moxi-new-lolly-apple-green-05f13-GREEN` | 9417864773878 | Not deleted | 2026-06-15 |
| 42154 | `moxi-new-lolly-clementine--ac263` | 9417865298166 | Not deleted | 2026-06-15 |
| 42157 | `moxi-new-lolly-pineapple-b-691a2` | 9417866215670 | Not deleted | 2026-06-15 |
| 42158 | `moxi-new-lolly-poppy-red-b-04032-RED` | 9417866412278 | Not deleted | 2026-06-15 |
| 42137 | `moxi-new-lolly-taffy-boots` | 9417860546806 | Not deleted | 2026-06-15 |
| 42134 | `riedell-120-award-skate-bo-717a9-WHITE` | 9417859629302 | Not deleted | 2026-06-15 |
| 42128 | `riedell-120-award-skate-bo-dffc1-WHITE` | 9417857761526 | Not deleted | 2026-06-15 |
| 42131 | `riedell-135-classic-skate--2be4c-TAN` | 9417858744566 | Not deleted | 2026-06-15 |
| 42132 | `riedell-135-classic-skate--5684b-BLACK` | 9417859006710 | Not deleted | 2026-06-15 |
| 42133 | `riedell-172-og-skate-boots-c91ea-BLACK` | 9417859301622 | Not deleted | 2026-06-15 |
| 42159 | `riedell-220-retro-skate-bo-11f97-BLACK` | 9417866608886 | Not deleted | 2026-06-15 |
| 42160 | `riedell-220-retro-skate-bo-34aa4-WHITE` | 9417866903798 | Not deleted | 2026-06-15 |
| 42161 | `riedell-220-retro-skate-bo-51eec-BLACK` | 9417867231478 | Not deleted | 2026-06-15 |
| 42138 | `riedell-220-retro-skate-bo-b96ee-WHITE` | 9417860841718 | Not deleted | 2026-06-15 |
| 42162 | `riedell-220-retro-skate-bo-e7adc-BLACK` | 9417867559158 | Not deleted | 2026-06-15 |
| 42163 | `riedell-220-retro-skate-bo-f40da-WHITE` | 9417867886838 | Not deleted | 2026-06-15 |
| 42166 | `riedell-297-pro-skate-boot-0520c-WHITE` | 9417868902646 | Not deleted | 2026-06-15 |
| 42139 | `riedell-297-pro-skate-boot-09389-WHITE` | 9417861136630 | Not deleted | 2026-06-15 |
| 42167 | `riedell-297-pro-skate-boot-5687c-BLACK` | 9417869263094 | Not deleted | 2026-06-15 |
| 42168 | `riedell-297-pro-skate-boot-c6d7d-WHITE` | 9417869558006 | Not deleted | 2026-06-15 |
| 42169 | `riedell-297-pro-skate-boot-d5f49-BLACK` | 9417869918454 | Not deleted | 2026-06-15 |
| 42170 | `riedell-297-pro-skate-boot-da857-BLACK` | 9417870213366 | Not deleted | 2026-06-15 |
| 42171 | `riedell-336-tribute-skate--49bb0-BLACK` | 9417870475510 | Not deleted | 2026-06-15 |
| 42172 | `riedell-336-tribute-skate--76fca-WHITE` | 9417870835958 | Not deleted | 2026-06-15 |
| 42173 | `riedell-336-tribute-skate--bb6f6-WHITE` | 9417871229174 | Not deleted | 2026-06-15 |
| 42174 | `riedell-336-tribute-skate--d60ca-BLACK` | 9417871491318 | Not deleted | 2026-06-15 |
| 42175 | `riedell-336-tribute-skate--edae3-WHITE` | 9417871786230 | Not deleted | 2026-06-15 |
| 42176 | `riedell-336-tribute-skate--fe5e3-BLACK` | 9417872113910 | Not deleted | 2026-06-15 |
| 42177 | `riedell-395-boot-black-wid-d2541-BLACK` | 9417905766646 | Not deleted | 2026-06-15 |
| 42140 | `riedell-910-flair-skate-bo-3d9d2-BLACK` | 9417861431542 | Not deleted | 2026-06-15 |
| 40179 | `FR-Freteletof4ls-nyer-ue-8A` | 9344554926326 | Not deleted | 2026-04-10 |
| 40181 | `FR-Fretrseletof4ls-ge-4A` | 9344554991862 | Not deleted | 2026-04-10 |
| 40192 | `Luminous-usedneeletof4ls-ck-5A` | 9344555516150 | Not deleted | 2026-04-10 |
| 40200 | `Luminous-usedneeletof4ls-enleow-5A` | 9344555843830 | Not deleted | 2026-04-10 |
| 40198 | `Luminous-usedneeletof4ls-go-5A` | 9344555712758 | Not deleted | 2026-04-10 |
| 40199 | `Luminous-usedneeletof4ls-leze-5A` | 9344555778294 | Not deleted | 2026-04-10 |
| 40188 | `Luminous-usedneeletof4ls-ondy-5A` | 9344555385078 | Not deleted | 2026-04-10 |
| 40194 | `Luminous-usedneeletof4ls-ry-5A` | 9344555581686 | Not deleted | 2026-04-10 |
| 50596 | `465535-387-050-parent` | 9460937228534 | Not deleted | 2026-07-14 |
| 38827 | `Echo-hoar3eles-00` | 9277472506102 | Errors while updating | 2026-02-11 |
| 38825 | `Echo-hoar4eles-80` | 9277472014582 | Errors while updating | 2026-02-11 |
| 39313 | `Rio-ioerstades-ra` | 9297982947574 | Errors while updating | 2026-03-03 |
| 42323 | `riedell-135-zone-skates-ta-fd536` | 9423302623478 | Not deleted | 2026-06-15 |

## Caveats

- **SWH Shopify only** (ChannelId 18). The other four Shopify stores (Venom, Icarus,
  Lobster, TWG B2B) were not swept.
- **Active Linnworks items only** — the sweep endpoint never returns archived items, so a
  dangling template on an archived item would not appear here.
- Verification covered the 783 listing ids whose template status was NOT "Listed". The
  839 dangling-candidate ids on "Listed" templates were not individually verified; the
  method for those is identical if you want them checked.
- Every id reported dangling was confirmed by a direct Shopify `nodes(ids:)` probe
  returning null. Products that were merely ARCHIVED or DRAFT return an object and were
  correctly excluded.
