# Como os exemplos funcionam

Os três clientes usam somente a
[API pública Moonu](https://dev.moonu.com.br/) e o WSS de áudio retornado por ela.
A [Moonu](https://moonu.com.br) conecta a chamada; o cliente adapta esse áudio
ao protocolo de um provedor de IA.

## Uma chamada, do começo ao fim

1. A aplicação solicita a conexão de um ramal existente e acompanha seus eventos.
2. Ao receber uma ligação, abre uma sessão de IA e solicita acesso temporário ao áudio.
3. Conecta o WSS Moonu, atende a chamada e espera a confirmação do atendimento.
4. Envia o áudio recebido à IA e reproduz as respostas no telefone.
5. Quando a ligação termina, fecha a sessão de IA e os recursos daquela chamada.

Para originar, a aplicação primeiro prepara o destino e conecta o áudio; depois
solicita a discagem. A IA não fala antes do atendimento. Não há rediscagem automática.

## Onde alterar cada parte

| Arquivo | Responsabilidade |
| --- | --- |
| `application.py` | Parâmetros, credenciais, atendimento/originação e fechamento da sessão |
| `client.py` | REST, eventos e registro Moonu; também contém o teste básico de áudio |
| `audio.py` | Quadros de áudio, fila limitada, envio cadenciado e ações protegidas |
| `realtime.py` | Eventos do protocolo realtime compartilhado por OpenAI e xAI |
| `profile.py` | Instruções do assistente Moonu e ferramentas de chamada |
| `knowledge.py` | Consulta limitada e somente de leitura aos sites públicos Moonu |
| `dtmf.py` | Envio de dígitos pela API Moonu e acompanhamento da confirmação de envio |
| `logs.py` | Logs JSON/texto com contexto por chamada |

Os diretórios de provedor implementam configuração, autenticação, eventos de
áudio e respostas das ferramentas. O ciclo telefônico não fica duplicado.
`AudioBridge` não conhece o protocolo OpenAI; Gemini herda somente as operações
comuns. Grok usa os eventos realtime compatíveis, mas sua configuração e o
agrupamento de ferramentas são próprios.

## Áudio e interrupção

A interface Moonu deste exemplo usa PCM16 mono de 24 kHz, em quadros de 20 ms.
Os fragmentos recebidos da IA são remontados e enviados no ritmo do áudio,
com filas limitadas para evitar crescimento indefinido da memória.

Interrupção real limpa o áudio pendente e avança a geração de playback da conexão
Moonu; os clientes descartam fragmentos já cancelados. O envio não prova que o
interlocutor ouviu o áudio, pois existem buffers e atrasos no caminho.

A fila suporta até 60 segundos. Ao atingir o limite, OpenAI/Grok solicitam
cancelamento da geração, preservando o áudio enfileirado; Gemini mantém o buffer
e descarta o excedente daquela resposta sem simular fala do usuário para cancelar.
Pode haver corte de uma resposta muito longa; mantenha respostas curtas.

## Personalizar sem misturar responsabilidades

Use `--instructions-file` para acrescentar contexto público simples.
Para trocar a persona ou as ferramentas, edite `profile.py` e sua execução na
aplicação; não coloque autenticação, banco de dados ou efeitos externos no prompt.
A consulta de demonstração permanece restrita aos sites públicos da Moonu.

Credenciais e a decisão de quais ferramentas executar pertencem à aplicação.
Dados retornados por páginas e ferramentas são referência, não instruções.
Valide argumentos e autorização antes de executar qualquer novo efeito externo.

A transferência atual usa um código e destino fixos (`#`, pausa, `500`).
Confirme com a administração da telefonia antes de testar. Não permita que a IA
envie números arbitrários sem validação e autorização próprias. O envio de DTMF
não confirma transferência concluída. Um envio parcial não é repetido automaticamente.

O desligamento aguarda o áudio pendente e verifica se o turno continua válido.
Não encerre por silêncio ou porque a palavra “tchau” apareceu fora de uma despedida.
As regras reduzem risco; não garantem interpretação perfeita do modelo.

## Logs e diagnóstico

O formato JSON é o padrão dos três executáveis. Exemplo ilustrativo:

```json
{"timestamp":"2026-10-23T12:00:00.000+00:00","level":"INFO","message":"Configuring AI session.","provider":"gemini","callId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage":"gemini_configure"}
```

As etapas distinguem conexão/configuração da IA, autorização de mídia Moonu,
conexão WSS, atendimento e conversa ativa. Erros de HTTP/TLS não incluem a
mensagem bruta da biblioteca, que poderia conter credenciais ou URLs sensíveis.
Não há modo de log bruto do provedor.

O contexto da chamada é isolado entre tarefas assíncronas. Logs e contadores não
são comprovantes de áudio ouvido nem cálculo de custo. Proteja e retenha logs
conforme a sua política, mesmo sem transcrições, pois podem conter identificadores.

## Fora do escopo

Estes módulos não implementam alta disponibilidade, gravação, discagem em massa,
credenciais multicliente, reconexão transparente ou garantia de entrega de áudio.
O ramal continua registrado após o término do script até uma desconexão explícita.
Para construir um serviço, acrescente supervisão, métricas, segurança, consentimento,
tratamento de falhas e limites de custo adequados ao seu produto.
