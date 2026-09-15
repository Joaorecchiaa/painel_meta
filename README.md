# Painel TV — Atingimento de Meta (Bruto) por Closer (deploy Vercel)

App Flask (serverless) que mostra, para cada closer, o valor **bruto**
vendido no mês (campo `value` do deal, sem multiplicador) contra a meta
financeira, numa barra quebrada de 10 em 10 até 100% (e além, se alguém
estourar a meta).

## Estrutura (pronta pro padrão de serverless function da Vercel)

```
painel-bruto-tv/
├── api/
│   └── index.py       ← app Flask completo (backend + HTML embutido)
├── vercel.json         ← manda TODAS as rotas pra api/index.py
├── requirements.txt    ← Flask + requests (sem pandas, pra manter leve)
├── .env.example
└── .gitignore
```

## Deploy na Vercel

1. Suba essa pasta inteira pra um repositório no GitHub (mantendo a estrutura acima).
2. Na Vercel: **Add New… → Project** → importe esse repositório.
3. Framework preset: deixe **Other** (a Vercel detecta o `vercel.json` e o runtime Python sozinha).
4. Em **Environment Variables**, adicione (mesmos nomes do `.env.example`):
   - `PIPE_API_KEY`
   - `FILTER_DEALS` (opcional — já tem default 74674)
   - `URL_METAS`
   - `URL_COLAB`
   - `SECRET_KEY` (opcional)
5. Deploy.
6. Abra `https://SEU-PROJETO.vercel.app/painel-bruto` na TV.
   (A rota `/` também redireciona pro mesmo painel.)

## Testando localmente antes de subir

O `.env` é carregado automaticamente (via `python-dotenv`), então não
precisa exportar nada manualmente no terminal — só editar o arquivo.

**Mac/Linux:**
```bash
cd painel-bruto-tv
pip install -r requirements.txt
cp .env.example .env   # edite com os valores reais (formato CHAVE=valor, sem aspas)
python3 -c "from api.index import app; app.run(port=5000)"
```

**Windows (PowerShell):**
```powershell
cd painel-bruto-tv
pip install -r requirements.txt
copy .env.example .env   # edite o .env com os valores reais (formato CHAVE=valor, sem aspas, sem $env:)
python -c "from api.index import app; app.run(port=5000)"
```

Acesse `http://localhost:5000/painel-bruto`.

Ou, se preferir testar com a própria CLI da Vercel:

```bash
npm i -g vercel
vercel dev
```

## Rotas

- `GET /painel-bruto` (e `GET /`) — o painel em si (HTML, auto-refresh a cada 60s)
- `GET /api/painel-bruto?mes=9&ano=2026` — JSON com os dados (mês/ano opcionais, default = mês atual)

## Regras implementadas

- Só entram closers: quem tem `meta_financeira > 0` e `meta_reuniao == 0` na planilha de metas (mesma definição usada no `server_17.py`), excluindo `priscila ribeiro`.
- Só entram closers dos squads/funis do time presencial: **ELITE, SNIPER, OLYMPUS** (também aparece como "MGM" na planilha de colaboradores) **e NAVIGATOR** — o squad de cada pessoa vem da coluna `subarea` da planilha de colaboradores (`URL_COLAB`). Quem não está nesses squads, ou não tem `subarea` cadastrada, fica de fora do painel.
- Valor "bruto" = soma do campo `value` dos negócios ganhos no mês, sem aplicar multiplicador.
- A barra só avança de bloco quando o closer bate **exatamente** aquele percentual (ex: 29,99% fica no bloco 20%, só valida 30% ao atingir 30% ou mais).
- Cores fixas por faixa: 0–60% vermelho, 70–90% amarelo, 100% verde, acima de 100% azul — a barra se estende automaticamente com blocos extras (110%, 120%...) se alguém passar da meta.

## Observação sobre a Vercel

O runtime Python da Vercel roda como função serverless (sem estado entre
requisições, sem processo contínuo). Isso é tranquilo aqui porque o painel
já busca os dados de novo a cada request (`/api/painel-bruto`) e o próprio
front-end refaz o fetch a cada 60s — não depende de nada guardado em
memória do servidor entre chamadas.