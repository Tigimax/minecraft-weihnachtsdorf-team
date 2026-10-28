/*!
 * Copyright (c) 2025 PLANKA Software GmbH
 * Licensed under the Fair Use License: https://github.com/plankanban/planka/blob/master/LICENSE.md
 * Enhanced with Authentik Forward-Auth SSO Auto-Login
 */

module.exports = {
  exits: {
    success: {
      responseType: 'view',
      viewTemplatePath: 'index',
    },
  },

  async fn() {
    const req = this.req;
    const res = this.res;
    const rawAuthUsername = req.headers['x-authentik-username'];
    const rawAuthEmail = req.headers['x-authentik-email'];
    const rawAuthName = req.headers['x-authentik-name'];

    let ssoToken = null;

    if (rawAuthUsername) {
      const authUsername = rawAuthUsername.trim().toLowerCase();
      const authEmail = rawAuthEmail ? rawAuthEmail.trim().toLowerCase() : null;
      const authName = rawAuthName ? rawAuthName.trim() : authUsername;

      try {
        let user = await User.qm.getOneActiveByEmailOrUsername(authUsername);
        if (!user && authEmail) {
          user = await User.qm.getOneActiveByEmailOrUsername(authEmail);
        }

        const termsSignature = '2796eedee33c0bc4592eb8f0d301acffe60ca01d81e9df4023cb32d05f724323';

        // Auto-provision if user does not exist yet
        if (!user) {
          user = await User.create({
            username: authUsername,
            email: authEmail || `${authUsername}@tigimax.mooo.com`,
            name: authName,
            role: User.Roles.ADMIN,
            termsSignature: termsSignature,
            termsAcceptedAt: new Date(),
            subscribeToOwnCards: false,
            subscribeToCardWhenCommenting: true,
            turnOffRecentCardHighlighting: false,
            enableFavoritesByDefault: true,
            defaultEditorMode: 'wysiwyg',
            defaultHomeView: 'groupedProjects',
            defaultProjectsOrder: 'byDefault',
            isDeactivated: false,
            autoLogoutMode: '30m',
          }).fetch();
        }

        if (user && !user.isDeactivated) {
          // Ensure terms signature is present to avoid terms modal gate
          if (!user.termsSignature) {
            await User.updateOne({ id: user.id }).set({
              termsSignature: termsSignature,
              termsAcceptedAt: new Date(),
            });
            user.termsSignature = termsSignature;
          }

          // Check if existing cookie session is still valid for this specific user
          let validSession = null;
          const existingToken = req.cookies && req.cookies.accessToken;
          if (existingToken) {
            try {
              const payload = sails.helpers.utils.verifyJwtToken(existingToken);
              if (payload && String(payload.sub) === String(user.id)) {
                validSession = await Session.qm.getOneUndeletedByAccessToken(existingToken);
              }
            } catch (err) {}
          }

          if (validSession) {
            ssoToken = existingToken;
          } else {
            // Generate a new token and create database session
            const { token: newAccessToken, payload: tokenPayload } = sails.helpers.utils.createJwtToken(user.id);
            await sails.helpers.sessions.createOne.with({
              values: {
                accessToken: newAccessToken,
                userId: user.id,
                remoteAddress: req.ip || '127.0.0.1',
                userAgent: req.headers['user-agent'] || '',
              },
            });

            ssoToken = newAccessToken;

            // Set browser cookies
            const isSecure = req.protocol === 'https' || req.headers['x-forwarded-proto'] === 'https';
            const expires = new Date(tokenPayload.exp * 1000);
            res.cookie('accessToken', newAccessToken, {
              expires,
              path: '/',
              secure: isSecure,
              sameSite: 'lax',
            });
            res.cookie('accessTokenVersion', '1', {
              expires,
              path: '/',
              sameSite: 'lax',
            });
          }
        }
      } catch (err) {
        sails.log.error('Authentik Forward-Auth Auto-Login error:', err);
      }
    }

    return {
      basePath: sails.config.custom.baseUrlPath,
      ssoToken,
    };
  },
};
