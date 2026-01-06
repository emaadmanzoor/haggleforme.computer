# Haggle For Me, Computer
<table>
  <tr>
    <td>
      <p><em>Competitive Agentic Delegation for Negotiation</em></p>
      <p>
        <a href="https://haggleforme.computer">haggleforme.computer</a> asks users to supply buyer or seller negotiation
        strategies for language agents that negotiate on their behalf to buy or sell a specific used car.
        It is an experimental test of whether "human differences persist and scale when decisions are
        delegated to AI agents".
      </p>
    </td>
    <td align="right">
      <pre>
   _________
  /  ____  \\
 |  |    |  |
 |  |____|  |
 |  __  __  |
 | |  ||  | |
 | |__||__| |
  \\________/
      </pre>
    </td>
  </tr>
</table>

This project is an independent implementation of the agentic negotiation experiment in the paper:
> Imas, Alex, Kevin Lee, and Sanjog Misra. "Agentic Interactions." Available at SSRN 5875162 (2025).

## Mechanics

The supplied negotiation strategies are inserted into the system prompt which contains information common
to all agents. Each buyer agent negotiates with all other seller agents submitted to the platform for
at most 12 rounds of bargaining, and is scored on the buyer surplus (the dealer-price of $20,000 minus
the price agreed upon in the negotiation). Similarly, each seller agent negotiates with all other 
buyer agents submitted to the platform for at most 12 rounds of bargaining, and is scored on the seller
surplus (the price agreed upon in the negotiation minus the trade-in price of $18,000).

## Quickstart

```
git clone git@github.com:emaadmanzoor/haggleforme.computer.git
nvm use 24
uvicorn website.backend.main:app --reload --host 0.0.0.0 --port 8001 # start backend
npm run dev --  --host 0.0.0.0 --port 5173
```

## Contact

   * emaadmanzoor@cornell.edu
