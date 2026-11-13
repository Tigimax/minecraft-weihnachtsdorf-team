<?php

namespace Pterodactyl\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Hash;
use Illuminate\Support\Str;
use Pterodactyl\Models\User;

class AuthentikSsoMiddleware
{
    /**
     * Handle incoming request from Authentik Forward-Auth reverse proxy.
     */
    public function handle(Request $request, Closure $next): mixed
    {
        $trustedIps = ['192.168.1.174', '127.0.0.1', '::1'];
        $remoteAddr = $request->server('REMOTE_ADDR');
        // Require direct TCP peer to be trusted if REMOTE_ADDR is present, preventing X-Forwarded-For spoofing
        if ($remoteAddr !== null && !in_array($remoteAddr, $trustedIps, true)) {
            $isTrusted = false;
        } else {
            $isTrusted = in_array($request->ip(), $trustedIps, true) || in_array($remoteAddr, $trustedIps, true);
        }

        if ($isTrusted && !$request->is('api/remote*')) {
            $headerUsername = $request->header('X-authentik-username');
            $headerEmail = $request->header('X-authentik-email');
            $headerGroups = $request->header('X-authentik-groups', '');
            $headerName = $request->header('X-authentik-name');

            if (!empty($headerUsername) || !empty($headerEmail)) {
                $rawUsername = !empty($headerUsername) ? trim($headerUsername) : explode('@', $headerEmail)[0];
                $cleanUsername = trim(preg_replace('/[^a-zA-Z0-9_\.-]/', '', strtolower($rawUsername)), '._-');
                if (strlen($cleanUsername) < 3) {
                    $cleanUsername = 'u_' . $cleanUsername;
                }

                $email = !empty($headerEmail) ? strtolower(trim($headerEmail)) : ($cleanUsername . '@tigimax.mooo.com');

                $groupsList = array_map(fn($g) => strtolower(trim($g)), explode(',', $headerGroups));
                $isAdmin = in_array('wd_admins', $groupsList, true) ||
                           in_array('weihnachtsdorf_admins', $groupsList, true) ||
                           in_array('authentik admins', $groupsList, true) ||
                           $cleanUsername === 'tigimax';

                $currentUser = Auth::user();
                if (!$currentUser || (strtolower($currentUser->username) !== $cleanUsername && strtolower($currentUser->email) !== $email)) {
                    $user = User::whereRaw('LOWER(username) = ?', [$cleanUsername])
                        ->orWhereRaw('LOWER(email) = ?', [$email])
                        ->first();

                    if (!$user) {
                        $firstName = $cleanUsername;
                        $lastName = 'Admin';
                        if (!empty($headerName)) {
                            $parts = explode(' ', trim($headerName), 2);
                            $firstName = $parts[0];
                            if (isset($parts[1]) && !empty($parts[1])) {
                                $lastName = $parts[1];
                            }
                        }

                        $user = new User();
                        $user->uuid = (string) Str::uuid();
                        $user->username = $cleanUsername;
                        $user->email = $email;
                        $user->name_first = $firstName;
                        $user->name_last = $lastName;
                        $user->language = 'en';
                        $user->root_admin = $isAdmin ? true : false;
                        $user->password = Hash::make(Str::random(32));
                        $user->save();
                    } else {
                        if ((bool) $user->root_admin !== $isAdmin) {
                            $user->root_admin = $isAdmin;
                            $user->save();
                        }
                    }

                    Auth::login($user, true);
                } else {
                    if ((bool) $currentUser->root_admin !== $isAdmin) {
                        $currentUser->root_admin = $isAdmin;
                        $currentUser->save();
                    }
                }

                // If user arrives at /auth/login while authenticated via Authentik, redirect to dashboard
                if ($request->is('auth/login') || $request->is('auth')) {
                    return redirect()->to('/');
                }
            }
        }

        // Handle logout: invalidate session and redirect to Authentik logout flow
        if ($request->is('auth/logout')) {
            Auth::logout();
            $request->session()->invalidate();
            $request->session()->regenerateToken();
            return redirect()->away('https://auth.tigimax.mooo.com/if/flow/default-invalidation-flow/');
        }

        return $next($request);
    }
}
