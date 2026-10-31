# Chaîne d'Or : jeu de quiz Android

Jeu de quiz télévisé inspiré du *Maillon faible* : vous jouez contre 5 adversaires gérés par l'ordinateur.

- **Chaîne de gains** : 20 → 50 → 100 → 200 → 300 → 450 → 600 → 800 → 1 000 €.
- **BANQUE** : met la chaîne à l'abri avant de répondre. Une mauvaise réponse casse la chaîne.
- **Manches chronométrées** (90 s, puis de plus en plus courtes).
- **Vote d'élimination** après chaque manche. En cas d'égalité, le joueur le plus solide tranche.
- **Manche finale à deux** avec gains doublés, puis un **duel** de 5 questions (avec mort subite).
- Plus de 200 questions de culture générale, plus du calcul mental généré à la volée.
- 3 niveaux de difficulté. Le meilleur gain est sauvegardé.

## Installer

Copiez `ChaineDOr.apk` sur le téléphone, ouvrez-le et autorisez l'installation d'applications de source inconnue.
Android 5.0 ou plus récent est requis.

## Reconstruire

Il faut le SDK Android (platform 34, build-tools 35) et un JDK :

```bash
ANDROID_HOME=/chemin/vers/android-sdk ./build.sh
```

Le jeu se trouve dans `assets/` (HTML/JS). Pour ajouter des questions, modifiez `assets/questions.js`.
Pour tester dans un navigateur, ouvrez `assets/index.html`.
