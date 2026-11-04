# omr-ingestor

Pipeline local que pega um PDF de partitura (uma parte, um de 6 instrumentos de sopro), passa por OMR e entrega um `.mscz` do MuseScore com o instrumento certo (transposição, clave, nome) e o "Estilo 2026" aplicado.

## Estado

**Esqueleto, nada implementado.** Este repo tem só documentação, a tabela de instrumentos e o schema do manifesto. Nenhum script roda ainda.

## Como vai rodar (planejado)

- Roda no Mac do Rafael (arm64), por linha de comando.
- Entrada: um `musica.json` (ver `docs/musica.schema.json` e `examples/musica.exemplo.json`) que lista os PDFs, o instrumento de cada um (informado no envio, nunca adivinhado) e o número de compassos esperado.
- Saída: um `.mscz` por parte + `status.json` por item, em `saidas/` (fora do git).
- Etapas e decisões: `docs/arquitetura.md`.

## PDFs não saem do Mac

Os PDFs são do Rafael (comprados ou de editora). Não entram no git (`.gitignore` ignora `*.pdf`), não vão para servidor nem para outra máquina. Testes fora do Mac usam só arquivos de domínio público ou sintéticos.

## Ferramentas externas e licenças

O pipeline chama programas instalados à parte, por linha de comando. Nenhum código deles é copiado ou modificado aqui.

| Ferramenta | Versão alvo | Licença | Uso |
|---|---|---|---|
| Audiveris | 5.11.0 | AGPL-3.0 | OMR: PDF → MusicXML (`-batch -export`) |
| MuseScore Studio | 4.7.5 | GPL v3 | MusicXML → `.mscz` com estilo (`-S`, `-o`) |

Se um dia virar serviço para outras pessoas, revisar a AGPL do Audiveris antes.
