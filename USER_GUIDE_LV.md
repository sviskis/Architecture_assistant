# Architecture Assistant — īsā rokasgrāmata

## Ieteicamā darba secība

1. Atver **New / Open Project** un izvēlies projekta mapi. Katram projektam tiek izveidota sava `.architecture_assistant` mape ar datubāzi, aģentu iestatījumiem, Cline apmaiņas failiem un pārskatiem.
2. Sadaļā **Project brief** ieraksti mērķi, esošo situāciju, ierobežojumus un gatavības kritērijus.
3. Sadaļā **Agent settings** izvēlies Architect A, Lead architect un Architect B nodrošinātājus, modeļus un API atslēgas. `Test Connection` pārbauda konkrēto vietu; `Save team settings` saglabā visu komandu.
4. Sadaļā **Discussion** izpildi posmus secīgi: Round 1, Lead Review, Round 2, Final Synthesis un Generate Architecture Proposal.
5. Sadaļā **Architecture** izlasi cilvēkam lasāmo dokumentu. Apstiprini to, pieprasi labojumu vai noraidi.
6. Pēc apstiprināšanas spied **Create Execution Plan**. Izlasi un, ja nepieciešams, rediģē JSON melnrakstu, tad spied **Validate & Import**.
7. Spied **Run Until Idle**. Asistents pārvieto darbplūsmu līdz nākamajam lēmumam vai publicē konkrēto uzdevumu Cline failu kanālā.
8. Sadaļā **Coding / Cline** nokopē sagatavoto instrukciju un iedod to Cline tajā pašā projekta mapē. Kad Cline uzraksta atskaiti norādītajā vietā, spied **Check report**, pēc tam **Run Until Idle**.
9. Sadaļā **Progress & checks** apstiprini cilvēka lēmumus, atrisini konfliktus un pārbaudi rezultātu. Beigās eksportē Markdown vai Excel pārskatu.

## Galvenās pogas

### Augšējā josla

- **Costs** — pēdējās sesijas un pakalpojumu izmaksu detaļas.
- **Logs** — operatora sesijas notikumi.
- **Clear Logs** — iztīra tikai redzamo sesijas žurnālu; audita ierakstus nedzēš.
- **Audit** — nemaināma cilvēka darbību un stāvokļu pāreju vēsture.
- **Risks** — atvērtie riski.
- **Reports** — izveidoto pārskatu saraksts.
- **Project Details** — projekta identitāte, režīms un stāvoklis.
- **Architecture Details** — asistenta determinētā arhitektūras bāzlīnija un pārbaude.

### Review

- **Run Architecture Review** — nosūta vienu jautājumu trim Quick Review konsultantiem. Tas ir padoms un nemaina darbplūsmu.
- **Test Connection** — pārbauda konkrētā konsultanta API savienojumu.
- **Save** — saglabā visu konsultantu konfigurāciju.
- **Details…** — pilna konsultanta atbilde, pierādījumi un izmaksas.

### Discussion

- **Run Round 1** — abi arhitekti neatkarīgi sagatavo risinājumu no tava brief.
- **Generate Lead Review** — Lead salīdzina abus risinājumus un nosauc vienprātību, konfliktus un jautājumus.
- **Run Round 2** — arhitekti vienu reizi pārskata savus risinājumus, redzot Lead kritiku un otra arhitekta strukturētos argumentus.
- **Generate Final Synthesis** — Lead izveido vienotu gala arhitektūru.
- **Generate Architecture Proposal** — saglabā sintēzi kā pārskatāmu DRAFT priekšlikumu.
- **Cancel Deliberation** — pārtrauc konkrēto diskusiju, saglabājot tās vēsturi.

### Architecture

- **Generate Architecture Proposal** — Quick Review rezultātu pārvērš arhitektūras priekšlikumā.
- **Approve** — cilvēks apstiprina dizainu; tikai pēc tam var veidot izpildes plānu.
- **Request Revision** — pieprasa jaunu priekšlikuma versiju ar norādīto atgriezenisko saiti.
- **Reject** — noraida priekšlikumu.

### Plan

- **Create Execution Plan** — no apstiprinātās arhitektūras izveido rediģējamu uzdevumu plānu atkarību secībā.
- **Load Plan** — izvēlas ārēji sagatavotu JSON plānu un vispirms parāda validācijas priekšskatījumu.
- **Import Plan / Confirm Import** — pēc apstiprināšanas atomāri importē plānu tukšā projekta datubāzē.
- **Validate & Import…** — validē rediģēto ģenerēto plānu un atver to pašu drošo importa apstiprinājumu.
- **Save JSON…** — saglabā melnrakstu failā bez darbplūsmas izmaiņām.

### Loop un Project

- **Run Until Idle** — virza procesu līdz gaidīšanai: cilvēka lēmumam, Cline atskaitei, blokatoram vai pabeigšanai.
- **Pause** — aptur projekta virzīšanu ar auditētu iemeslu.
- **Resume** — atsāk apturētu projektu ar auditētu iemeslu.

### Approval un Step control

- **Approve / Reject** — izlemj soli, kas gaida cilvēka apstiprinājumu.
- **Unblock** — turpina bloķētu soli pēc tam, kad problēma tiešām novērsta.
- **Resolve** — atzīmē konflikta atrisinājumu.
- **Abort** — termināli pārtrauc pašreizējo soli.

### Supervisor

- **Analyze Report** — analizē pašreizējo Cline atskaiti.
- **Approve & Send** — publicē pārbaudītu labošanas norādi Cline failu kanālā.
- **Reject Directive** — noraida sagatavoto norādi.
- **Waive** — konkrētajai atskaitei izlaiž uzraudzības prasību.
- **Escalate** — nodod lēmumu cilvēkam.

Pašreizējais komplektācijā iekļautais Supervisor ir **offline scripted supervisor**, nevis dzīvs AI vadītājs. Interfeiss to norāda atklāti.

### Reports un Monitoring

- **Export Markdown / Export Excel** — izveido pārskata failu.
- **Open reports folder** — atver pārskatu mapi.
- **Refresh / Check report** — pārlasa kanonisko stāvokli un Cline atskaites statusu.
- **View Monitor Snapshot** — pilns pašreizējais stāvokļa momentuzņēmums.
- **Reconnect** — no jauna izveido savienojumu ar projekta datubāzi; neko nelabo un nepārraksta.

## Kā piesaistīt Cline

Architecture Assistant un Cline sazinās ar failiem. Asistents publicē kontekstu un `*_task.json` mapē `.architecture_assistant/cline/to_cline`. Cline izpilda tikai šo soli un publicē `*_report.json` mapē `.architecture_assistant/cline/from_cline` pēc task failā dotās shēmas.

Sadaļa **Coding / Cline** parāda precīzu task, context, directive un report ceļu, kā arī sagatavotu instrukciju kopēšanai. “Task published” nozīmē tikai to, ka fails ir gatavs; tas vēl nepierāda, ka Cline ir sācis darbu. “Report received and structurally validated” nozīmē, ka atskaite ir lasāma; gala spriedumu joprojām izdara determinētā darbplūsma un cilvēks.

Asistents vada kodēšanas procesu, sadalot darbu apstiprinātā plāna soļos, publicējot vienu aktuālo uzdevumu, pārbaudot atskaites identitāti un arhitektūras noteikumus, pieprasot apstiprinājumus un saglabājot auditu. Cline paliek koda izpildītājs un pats nevar apstiprināt arhitektūru vai atzīmēt rezultātu kā verificētu.
